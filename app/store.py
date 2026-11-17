import sqlite3, json, hashlib
from datetime import datetime, timezone
from pathlib import Path
from app.errors import BusinessError

OPERATIONS = {'ADD', 'UPDATE', 'WITHDRAW'}
# 更正生命周期：待处理（未证实，绝不进入计算）→ 已证实（证据过闸，待批次构建）→ 已生效（已产出批次）；证据不过 → 已拒绝
PENDING, VERIFIED, APPLIED, REJECTED = '待处理', '已证实', '已生效', '已拒绝'
EVENT_SUBMIT, EVENT_VERIFY, EVENT_REJECT, EVENT_REPLACE, EVENT_WAIT, EVENT_RETRY = (
    '登记', '证实', '拒绝', '替换生效', '继续等待', '重试')


def connect(path):
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("""CREATE TABLE IF NOT EXISTS corrections (
        correction_id TEXT PRIMARY KEY,
        root_run_id TEXT NOT NULL,
        base_run_id TEXT NOT NULL,
        op TEXT NOT NULL,
        target TEXT NOT NULL,
        payload TEXT NOT NULL,
        before_value TEXT,
        evidence_ref TEXT NOT NULL,
        evidence_hash TEXT NOT NULL,
        evidence_text TEXT NOT NULL,
        state TEXT NOT NULL,
        reason TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        produced_run_id TEXT,
        submitted_at TEXT NOT NULL,
        verified_at TEXT,
        payload_hash TEXT NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS correction_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        correction_id TEXT NOT NULL,
        event TEXT NOT NULL,
        detail TEXT NOT NULL,
        at TEXT NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS active_batches (
        root_run_id TEXT PRIMARY KEY,
        active_run_id TEXT NOT NULL,
        updated_at TEXT NOT NULL)""")
    return con


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, result):
    run_id = result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)',
                    (run_id, result['manifest']['sha256'], json.dumps(result, ensure_ascii=False)))
        # 首次出现的批次即成为其血缘根的活跃批次；更正替换只走 replace_active，不改写本指针。
        root = result.get('lineage', {}).get('root_run_id', run_id)
        con.execute('INSERT OR IGNORE INTO active_batches(root_run_id,active_run_id,updated_at) VALUES(?,?,?)',
                    (root, run_id, now()))
    return get(path, run_id)


def get(path, run_id):
    with connect(path) as con:
        row = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise BusinessError('NOT_FOUND', '批次不存在', 404)
        result = json.loads(row['result'])
        result.update(revision=row['revision'], status=row['status'])
        return result


def history(path):
    with connect(path) as con:
        rows = [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]
        active = {r['root_run_id']: r['active_run_id']
                  for r in con.execute('SELECT root_run_id,active_run_id FROM active_batches')}
        for item in rows:
            run = json.loads(con.execute('SELECT result FROM runs WHERE id=?', (item['id'],)).fetchone()['result'])
            root = run.get('lineage', {}).get('root_run_id', item['id'])
            item['root_run_id'] = root
            item['active'] = active.get(root, item['id']) == item['id']
        return rows


def review(path, run_id, revision):
    with connect(path) as con:
        cur = con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',
                          ('已复核', run_id, revision))
        if cur.rowcount != 1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone():
                raise BusinessError('NOT_FOUND', '批次不存在', 404)
            raise BusinessError('STALE_VERSION', '复核版本已更新，请刷新', 409)
    return get(path, run_id)


# ---------------- 迟到更正：追加式登记、证据闸门、批次替换 ----------------

def canonical_payload(op, target, payload):
    return json.dumps({'op': op, 'target': target, 'payload': payload},
                      ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def derive_correction_id(root_run_id, op, target, payload, evidence_hash):
    material = '|'.join([root_run_id, canonical_payload(op, target, payload), evidence_hash])
    return 'CR-' + hashlib.sha256(material.encode()).hexdigest()[:12]


def log_event(con, correction_id, event, detail):
    con.execute('INSERT INTO correction_events(correction_id,event,detail,at) VALUES(?,?,?,?)',
                (correction_id, event, detail, now()))


def submit_correction(path, correction):
    """登记一条迟到更正。同号重试不新增结果（幂等），不同载荷占用同号则冲突。"""
    cid = correction['correction_id']
    with connect(path) as con:
        existing = con.execute('SELECT * FROM corrections WHERE correction_id=?', (cid,)).fetchone()
        if existing:
            if existing['payload_hash'] != correction['payload_hash']:
                raise BusinessError('CORRECTION_CONFLICT', '更正号已被不同载荷占用，请使用新更正号', 409)
            log_event(con, cid, EVENT_RETRY,
                      f"同一更正重试，未新增结果（状态：{existing['state']}）")
            return get_correction(con, cid), False
        con.execute("""INSERT INTO corrections(correction_id,root_run_id,base_run_id,op,target,payload,
            before_value,evidence_ref,evidence_hash,evidence_text,state,reason,attempts,last_error,
            produced_run_id,submitted_at,verified_at,payload_hash)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (cid, correction['root_run_id'], correction['base_run_id'], correction['op'],
                     correction['target'], json.dumps(correction['payload'], ensure_ascii=False),
                     correction['before_value'], correction['evidence_ref'], correction['evidence_hash'],
                     correction['evidence_text'], PENDING, correction['reason'], 0, None, None,
                     now(), None, correction['payload_hash']))
        log_event(con, cid, EVENT_SUBMIT,
                  f"更正登记为{PENDING}：证据未证实前不参与复检与锁库判断，当前批次继续生效")
        return get_correction(con, cid), True


def get_correction(con, cid):
    row = con.execute('SELECT * FROM corrections WHERE correction_id=?', (cid,)).fetchone()
    if not row:
        raise BusinessError('CORRECTION_NOT_FOUND', '更正记录不存在', 404)
    data = dict(row)
    data['payload'] = json.loads(row['payload'])
    data['events'] = [dict(r) for r in con.execute(
        'SELECT event,detail,at FROM correction_events WHERE correction_id=? ORDER BY id', (cid,))]
    return data


def list_corrections(path, root_run_id=None):
    with connect(path) as con:
        sql = 'SELECT correction_id FROM corrections'
        args = ()
        if root_run_id:
            sql += ' WHERE root_run_id=?'
            args = (root_run_id,)
        sql += ' ORDER BY rowid'
        return [get_correction(con, r['correction_id'])
                for r in con.execute(sql, args)]


def get_correction_safe(path, cid):
    with connect(path) as con:
        return get_correction(con, cid)


def set_verdict(path, cid, state, reason):
    with connect(path) as con:
        row = con.execute('SELECT state FROM corrections WHERE correction_id=?', (cid,)).fetchone()
        if not row:
            raise BusinessError('CORRECTION_NOT_FOUND', '更正记录不存在', 404)
        con.execute('UPDATE corrections SET state=?,reason=?,verified_at=? WHERE correction_id=?',
                    (state, reason, now(), cid))
        log_event(con, cid, EVENT_VERIFY if state == VERIFIED else EVENT_REJECT, reason)
        return get_correction(con, cid)


def mark_attempt(path, cid, error=None, produced_run_id=None):
    with connect(path) as con:
        if error is None:
            con.execute('UPDATE corrections SET state=?,attempts=attempts+1,last_error=NULL,produced_run_id=? WHERE correction_id=?',
                        (APPLIED, produced_run_id, cid))
            log_event(con, cid, EVENT_REPLACE, f'新批次 {produced_run_id} 替换当前可用批次')
        else:
            con.execute('UPDATE corrections SET attempts=attempts+1,last_error=? WHERE correction_id=?',
                        (error, cid))
            log_event(con, cid, EVENT_WAIT, f'替换失败，保留当前旧版结果：{error}')
        return get_correction(con, cid)


def active_run(path, root_run_id):
    with connect(path) as con:
        row = con.execute('SELECT * FROM active_batches WHERE root_run_id=?', (root_run_id,)).fetchone()
        return dict(row) if row else None


def replace_active(path, root_run_id, new_run_id, detail):
    """仅在新批次构建成功后调用；旧批次行不删除、不覆盖，血缘可回溯。"""
    with connect(path) as con:
        old = con.execute('SELECT active_run_id FROM active_batches WHERE root_run_id=?',
                          (root_run_id,)).fetchone()
        old_id = old['active_run_id'] if old else None
        con.execute("""INSERT INTO active_batches(root_run_id,active_run_id,updated_at) VALUES(?,?,?)
            ON CONFLICT(root_run_id) DO UPDATE SET active_run_id=excluded.active_run_id,updated_at=excluded.updated_at""",
                    (root_run_id, new_run_id, now()))
        return old_id


def lineage(path, root_run_id):
    with connect(path) as con:
        rows = con.execute("""SELECT id,result FROM runs ORDER BY rowid""").fetchall()
        chain = []
        for r in rows:
            result = json.loads(r['result'])
            if result.get('lineage', {}).get('root_run_id', r['id']) != root_run_id:
                continue
            chain.append({'run_id': r['id'], 'lineage': result.get('lineage', {}),
                          'balance': result.get('balance', {}), 'generated_at': result['manifest']['generated_at']})
        active = active_run(path, root_run_id)
        return {'root_run_id': root_run_id,
                'active_run_id': active['active_run_id'] if active else root_run_id,
                'chain': chain}
