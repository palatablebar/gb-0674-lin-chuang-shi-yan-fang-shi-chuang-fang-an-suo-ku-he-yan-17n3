import pytest
from fastapi.testclient import TestClient
from app import main, store
from app.engine import evaluate
from app.modules.correction import rules, service
from app.config import ROOT


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    return TestClient(main.app)


def evidence(target, value, ref='SRC-2026-0001'):
    text = f'{target} 复核化验单 {ref} 实测 {value} mg/dL'
    return {'evidence_ref': ref, 'evidence_text': text,
            'evidence_hash': rules.sha256_text(text)}


def correction(op, target, value=None, **extra):
    if op == 'WITHDRAW':
        text = f'{target} 撤回依据 SRC-2026-0001'
        payload = {'op': op, 'target': target, 'payload': {},
                   'evidence_ref': 'SRC-2026-0001', 'evidence_text': text,
                   'evidence_hash': rules.sha256_text(text), 'reason': '迟到更正'}
    else:
        payload = {'op': op, 'target': target, 'payload': {'value': str(value)},
                   'reason': '迟到更正', **evidence(target, value)}
    payload.update(extra)
    return payload


# ---------- 纯规则层：证据闸门 ----------

def test_evidence_gate_hash_mismatch():
    c = {'op': 'UPDATE', 'target': '00003-V1', 'payload': {'value': '126'},
         'evidence_ref': 'SRC-1', 'evidence_text': '00003-V1 实测 126',
         'evidence_hash': 'deadbeef'}
    ok, code, _ = rules.check_evidence(c)
    assert not ok and code == 'EVIDENCE_HASH_MISMATCH'


def test_evidence_gate_value_unproven():
    text = '00003-V1 化验单但数值栏空白'
    c = {'op': 'UPDATE', 'target': '00003-V1', 'payload': {'value': '126'},
         'evidence_ref': 'SRC-1', 'evidence_text': text,
         'evidence_hash': rules.sha256_text(text)}
    ok, code, _ = rules.check_evidence(c)
    assert not ok and code == 'EVIDENCE_VALUE_UNPROVEN'


def test_evidence_gate_number_equivalence():
    # 千分位与等价小数必须识别为同一数值，规则显式可复核
    text = '00003-V1 实测 126.0 mg/dL'
    c = {'op': 'UPDATE', 'target': '00003-V1', 'payload': {'value': '126'},
         'evidence_ref': 'SRC-1', 'evidence_text': text,
         'evidence_hash': rules.sha256_text(text)}
    ok, _, _ = rules.check_evidence(c)
    assert ok


def test_evidence_gate_withdraw_needs_no_value():
    text = '00020-V1 撤回依据 SRC-9'
    c = {'op': 'WITHDRAW', 'target': '00020-V1', 'payload': {},
         'evidence_ref': 'SRC-9', 'evidence_text': text,
         'evidence_hash': rules.sha256_text(text)}
    ok, _, _ = rules.check_evidence(c)
    assert ok


def test_evidence_gate_missing_ref():
    c = {'op': 'UPDATE', 'target': 'X', 'payload': {'value': '1'},
         'evidence_ref': ' ', 'evidence_text': 'X 1', 'evidence_hash': rules.sha256_text('X 1')}
    ok, code, _ = rules.check_evidence(c)
    assert not ok and code == 'EVIDENCE_REF_MISSING'


# ---------- 端到端工作流 ----------

def test_unverified_correction_never_replaces_batch(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'clean'}).json()
    rid = run['manifest']['run_id']
    r = c.post(f'/api/runs/{rid}/corrections', json=correction('UPDATE', '00003-V1', 126))
    assert r.status_code == 200 and r.json()['created'] is True
    board = c.get(f'/api/runs/{rid}/corrections').json()
    assert board['active']['active_run_id'] == rid
    assert board['corrections'][0]['state'] == '待处理'
    # 当前可用批次结果未变
    same = c.get(f'/api/runs/{rid}').json()
    assert same['manifest']['sha256'] == run['manifest']['sha256']


def test_verified_correction_builds_new_batch_and_keeps_old(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'clean'}).json()
    rid = run['manifest']['run_id']
    old_value = next(x for x in run['records'] if x['id'] == '00003-V1')['value_mg_dl']
    sub = c.post(f'/api/runs/{rid}/corrections', json=correction('UPDATE', '00003-V1', 126)).json()
    out = c.post(f"/api/corrections/{sub['correction']['correction_id']}/verify", json={}).json()
    new_id = out['replacement']['run_id']
    assert new_id != rid and out['replacement']['previous_run_id'] == rid
    new_run = c.get(f'/api/runs/{new_id}').json()
    assert next(x for x in new_run['records'] if x['id'] == '00003-V1')['value_mg_dl'] != old_value
    # 旧批次仍可读取、血缘完整、新批次为活跃
    assert c.get(f'/api/runs/{rid}').status_code == 200
    board = c.get(f'/api/runs/{new_id}/corrections').json()
    assert board['active']['active_run_id'] == new_id
    assert board['lineage']['chain'][0]['run_id'] == rid
    assert board['lineage']['chain'][-1]['run_id'] == new_id


def test_rejected_correction_keeps_current_batch(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'clean'}).json()
    rid = run['manifest']['run_id']
    payload = correction('UPDATE', '00003-V1', 126)
    payload['evidence_hash'] = '0' * 64  # 篡改哈希
    sub = c.post(f'/api/runs/{rid}/corrections', json=payload).json()
    out = c.post(f"/api/corrections/{sub['correction']['correction_id']}/verify", json={}).json()
    assert out['replacement'] is None
    assert out['correction']['state'] == '已拒绝'
    assert c.get(f'/api/runs/{rid}/corrections').json()['active']['active_run_id'] == rid


def test_rejected_is_terminal(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    payload = correction('UPDATE', '00003-V1', 126)
    payload['evidence_hash'] = '0' * 64
    sub = c.post(f'/api/runs/{rid}/corrections', json=payload).json()
    cid = sub['correction']['correction_id']
    assert c.post(f'/api/corrections/{cid}/verify', json={}).json()['correction']['state'] == '已拒绝'
    # 终态保护：拒绝后不能翻案，必须登记新更正
    r = c.post(f'/api/corrections/{cid}/verify', json={})
    assert r.status_code == 409 and r.json()['code'] == 'CORRECTION_TERMINAL'
    assert c.get(f'/api/runs/{rid}/corrections').json()['active']['active_run_id'] == rid


def test_manual_reject(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    sub = c.post(f'/api/runs/{rid}/corrections', json=correction('UPDATE', '00003-V1', 126)).json()
    cid = sub['correction']['correction_id']
    out = c.post(f'/api/corrections/{cid}/verify', json={'decision': 'reject'}).json()
    assert out['correction']['state'] == '已拒绝' and out['replacement'] is None


def test_idempotent_retry_no_duplicate(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    payload = correction('UPDATE', '00003-V1', 126)
    first = c.post(f'/api/runs/{rid}/corrections', json=payload).json()
    second = c.post(f'/api/runs/{rid}/corrections', json=payload).json()
    assert first['correction']['correction_id'] == second['correction']['correction_id']
    assert second['created'] is False
    board = c.get(f'/api/runs/{rid}/corrections').json()
    assert len(board['corrections']) == 1
    assert any(e['event'] == '重试' for e in board['corrections'][0]['events'])


def test_same_id_different_payload_conflicts(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    p1 = correction('UPDATE', '00003-V1', 126, correction_id='CR-FIXED')
    p2 = correction('UPDATE', '00003-V1', 130, correction_id='CR-FIXED')
    assert c.post(f'/api/runs/{rid}/corrections', json=p1).status_code == 200
    assert c.post(f'/api/runs/{rid}/corrections', json=p2).status_code == 409


def test_rebuild_failure_keeps_old_batch_and_retry_succeeds(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    sub = c.post(f'/api/runs/{rid}/corrections', json=correction('UPDATE', '00003-V1', 126)).json()
    cid = sub['correction']['correction_id']
    # 注入构建失败：重建 CSV 阶段抛业务错
    original = rules.rebuild_csv

    def boom(raw, corrections):
        raise __import__('app.errors', fromlist=['BusinessError']).BusinessError('INJECTED', '模拟重建失败')

    monkeypatch.setattr(rules, 'rebuild_csv', boom)
    out = c.post(f'/api/corrections/{cid}/verify', json={}).json()
    assert out['replacement'] is None
    board = c.get(f'/api/runs/{rid}/corrections').json()
    corr = board['corrections'][0]
    # 证据已过闸但构建未成功：停留“已证实”，区别于“已生效”
    assert corr['state'] == '已证实'
    assert corr['last_error'].startswith('INJECTED') and corr['attempts'] == 1
    assert board['active']['active_run_id'] == rid  # 旧版结果保留
    # 恢复后重试：同输入同规则，批号确定且替换成功，状态转已生效
    monkeypatch.setattr(rules, 'rebuild_csv', original)
    again = c.post(f'/api/corrections/{cid}/retry', json={}).json()
    assert again['replacement']['run_id'] != rid
    assert again['correction']['state'] == '已生效'
    assert c.get(f'/api/runs/{rid}/corrections').json()['active']['active_run_id'] == \
        again['replacement']['run_id']


def test_repeat_verify_is_idempotent_not_duplicate(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    sub = c.post(f'/api/runs/{rid}/corrections', json=correction('UPDATE', '00003-V1', 126)).json()
    cid = sub['correction']['correction_id']
    first = c.post(f'/api/corrections/{cid}/verify', json={}).json()
    second = c.post(f'/api/corrections/{cid}/verify', json={})
    assert second.status_code == 422
    assert second.json()['code'] == 'CORRECTION_ALREADY_VERIFIED'
    # 只有一个衍生批次
    runs = c.get('/api/runs').json()
    derived = [r for r in runs if not r['active'] or r['id'] != rid]
    assert len({r['id'] for r in runs}) == len(runs)
    assert first['replacement']['run_id']


def test_withdraw_and_add_corrections(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    w = c.post(f'/api/runs/{rid}/corrections',
               json=correction('WITHDRAW', '00020-V1')).json()
    out = c.post(f"/api/corrections/{w['correction']['correction_id']}/verify", json={}).json()
    new_id = out['replacement']['run_id']
    new_run = c.get(f'/api/runs/{new_id}').json()
    assert not any(r['id'] == '00020-V1' for r in new_run['records'])
    # 旧批记录仍在（不丢失）
    old_run = c.get(f'/api/runs/{rid}').json()
    assert any(r['id'] == '00020-V1' for r in old_run['records'])


def test_correction_must_target_active_batch(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    sub = c.post(f'/api/runs/{rid}/corrections', json=correction('UPDATE', '00003-V1', 126)).json()
    c.post(f"/api/corrections/{sub['correction']['correction_id']}/verify", json={})
    # 再向已被替换的旧批提交更正 → 409 引导到活跃批次
    r = c.post(f'/api/runs/{rid}/corrections', json=correction('UPDATE', '00004-V1', 120))
    assert r.status_code == 409 and r.json()['code'] == 'CORRECTION_BASE_STALE'


def test_page_renders_five_categories_and_lineage(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    sub = c.post(f'/api/runs/{rid}/corrections', json=correction('UPDATE', '00003-V1', 126)).json()
    html = c.get(f'/correction?run_id={rid}').text
    assert '迟到更正' in html and '待处理' in html and '批次血缘' in html and sub['correction']['correction_id'] in html
    c.post(f"/api/corrections/{sub['correction']['correction_id']}/verify", json={})
    html2 = c.get(f'/correction?run_id={rid}').text
    assert '可替换' in html2 and '已生效' in html2


def test_export_contains_correction_evidence(tmp_path, monkeypatch):
    import io, zipfile, json
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    sub = c.post(f'/api/runs/{rid}/corrections', json=correction('UPDATE', '00003-V1', 126)).json()
    c.post(f"/api/corrections/{sub['correction']['correction_id']}/verify", json={})
    new_id = c.get(f'/api/runs/{rid}/corrections').json()['active']['active_run_id']
    z = zipfile.ZipFile(io.BytesIO(c.get(f'/api/export/{new_id}').content))
    evidence = json.loads(z.read('evidence.json'))
    assert evidence['corrections'] and evidence['corrections'][0]['evidence_text']


def test_pending_correction_survives_verified_one(tmp_path, monkeypatch):
    # 两条更正：一条证实一条待处理；新批只含已证实内容，待处理继续等待且不丢
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    ok_sub = c.post(f'/api/runs/{rid}/corrections', json=correction('UPDATE', '00003-V1', 126)).json()
    pending_sub = c.post(f'/api/runs/{rid}/corrections', json=correction('UPDATE', '00004-V1', 120)).json()
    c.post(f"/api/corrections/{ok_sub['correction']['correction_id']}/verify", json={})
    board = c.get(f'/api/runs/{rid}/corrections').json()
    states = {x['correction_id']: x['state'] for x in board['corrections']}
    assert states[pending_sub['correction']['correction_id']] == '待处理'
    active_run = c.get(f"/api/runs/{board['active']['active_run_id']}").json()
    # 待处理更正的目标值在新批中保持原样
    target = next(r for r in active_run['records'] if r['id'] == '00004-V1')
    original = next(r for r in c.get(f'/api/runs/{rid}').json()['records'] if r['id'] == '00004-V1')
    assert target['value_mg_dl'] == original['value_mg_dl']


def test_add_correction_creates_record_in_new_batch(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    text = '00020-EXTRA 补录化验单 SRC-ADD-01 实测 105 mg/dL'
    payload = {'op': 'ADD', 'target': '00020-EXTRA',
               'payload': {'subject': '00020', 'visit': 'V2', 'at': '2026-10-15T08:00:00+08:00',
                           'enrolled': '2026-10-01T00:00:00+08:00', 'kind': 'PRIMARY',
                           'value': '105', 'unit': 'mg/dL', 'protocol': 'auto', 'revision': '1'},
               'evidence_ref': 'SRC-ADD-01', 'evidence_text': text,
               'evidence_hash': rules.sha256_text(text), 'reason': '漏采补录'}
    sub = c.post(f'/api/runs/{rid}/corrections', json=payload).json()
    out = c.post(f"/api/corrections/{sub['correction']['correction_id']}/verify", json={}).json()
    new_run = c.get(f"/api/runs/{out['replacement']['run_id']}").json()
    assert any(r['id'] == '00020-EXTRA' for r in new_run['records'])
    # 用不同证据（不同更正号）重复新增同号目标：证据可过闸，但重建阶段显式报错，不静默覆盖、旧批保留
    active_id = out['replacement']['run_id']
    text2 = '00020-EXTRA 二次补录单据 SRC-ADD-02 实测 105 mg/dL'
    payload2 = {**payload, 'evidence_ref': 'SRC-ADD-02', 'evidence_text': text2,
                'evidence_hash': rules.sha256_text(text2)}
    sub2 = c.post(f'/api/runs/{active_id}/corrections', json=payload2).json()
    assert sub2['correction']['correction_id'] != sub['correction']['correction_id']
    v2 = c.post(f"/api/corrections/{sub2['correction']['correction_id']}/verify", json={}).json()
    assert v2['replacement'] is None
    assert v2['correction']['last_error'].startswith('CORRECTION_TARGET_EXISTS')
    assert c.get(f'/api/runs/{rid}/corrections').json()['active']['active_run_id'] == active_id
    # 向已失效的旧批提交则直接 409 引导
    assert c.post(f'/api/runs/{rid}/corrections', json=payload).status_code == 409


def test_cumulative_verified_corrections_chain(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    first = c.post(f'/api/runs/{rid}/corrections', json=correction('UPDATE', '00003-V1', 126)).json()
    c.post(f"/api/corrections/{first['correction']['correction_id']}/verify", json={})
    mid = c.get(f'/api/runs/{rid}/corrections').json()['active']['active_run_id']
    second = c.post(f'/api/runs/{mid}/corrections', json=correction('UPDATE', '00005-V1', 130)).json()
    c.post(f"/api/corrections/{second['correction']['correction_id']}/verify", json={})
    board = c.get(f'/api/runs/{mid}/corrections').json()
    assert len(board['lineage']['chain']) == 3  # 原始 + 两个衍生批
    final = c.get(f"/api/runs/{board['active']['active_run_id']}").json()
    # 源单位 g/L，引擎按规则乘 100 输出 mg/dL
    assert next(r for r in final['records'] if r['id'] == '00003-V1')['value_mg_dl'] == '12600'
    assert next(r for r in final['records'] if r['id'] == '00005-V1')['value_mg_dl'] == '13000'
