import base64
from app import store
from app.engine import evaluate
from app.errors import BusinessError
from app.modules.correction import rules


def _require(condition, code, message, status=422):
    if not condition:
        raise BusinessError(code, message, status)


def _validated(payload):
    _require(isinstance(payload, dict), 'BAD_PAYLOAD', '请求体须为对象')
    op = payload.get('op')
    _require(op in rules.OPERATIONS, 'CORRECTION_OP', "动作 op 仅支持 ADD / UPDATE / WITHDRAW")
    target = str(payload.get('target', '')).strip()
    _require(target, 'CORRECTION_TARGET', '目标记录 target 不能为空')
    patch = payload.get('payload', {})
    _require(isinstance(patch, dict), 'CORRECTION_PAYLOAD', '更正载荷 payload 须为对象')
    evidence_text = payload.get('evidence_text', '')
    evidence_ref = str(payload.get('evidence_ref', '')).strip()
    _require(isinstance(evidence_text, str) and evidence_text.strip(),
             'EVIDENCE_TEXT_MISSING', '必须提供证据原文，更正前后证据都要可追溯')
    evidence_hash = payload.get('evidence_hash') or rules.sha256_text(evidence_text)
    _require(isinstance(evidence_hash, str), 'EVIDENCE_HASH_BAD', '证据哈希须为字符串')
    reason = str(payload.get('reason', '')).strip() or '迟到更正（未填写原因）'
    return {'op': op, 'target': target, 'payload': patch, 'evidence_ref': evidence_ref,
            'evidence_text': evidence_text, 'evidence_hash': evidence_hash, 'reason': reason}


def _find_before(run, target):
    for row in run['records']:
        if row['id'] == target:
            return row.get('value', '')
    return ''


def submit(path, run_id, payload):
    """登记更正：立即留痕但不生效。未证实内容绝不替换当前可用批次。"""
    base = store.get(path, run_id)
    _require('input_snapshot' in base, 'INPUT_SNAPSHOT_MISSING', '基础批次缺少源快照，无法承接更正')
    form = _validated(payload)
    root = base.get('lineage', {}).get('root_run_id', run_id)
    active = store.active_run(path, root)
    active_id = active['active_run_id'] if active else run_id
    _require(active_id == run_id, 'CORRECTION_BASE_STALE',
             f'该批次已被 {active_id} 替换，更正须提交到当前活跃批次', 409)
    payload_hash = store.canonical_payload(form['op'], form['target'], form['payload'])
    cid = str(payload.get('correction_id', '')).strip() or \
        store.derive_correction_id(root, form['op'], form['target'], form['payload'], form['evidence_hash'])
    record = {'correction_id': cid, 'root_run_id': root, 'base_run_id': run_id,
              'op': form['op'], 'target': form['target'], 'payload': form['payload'],
              'before_value': _find_before(base, form['target']),
              'evidence_ref': form['evidence_ref'], 'evidence_hash': form['evidence_hash'],
              'evidence_text': form['evidence_text'], 'reason': form['reason'],
              'payload_hash': payload_hash}
    saved, created = store.submit_correction(path, record)
    return saved, created


def _build_from_verified(path, root_run_id):
    """用血缘根的不可变快照 + 全部已证实更正重建批次。失败抛错，由调用方保证活跃指针不动。"""
    root = store.get(path, root_run_id)
    # 累积语义：每次重建都从血缘根快照重放全部已过证据闸门的更正（已证实 + 已生效），
    # 待处理与已拒绝永不参与。
    gated = (store.VERIFIED, store.APPLIED)
    verified = [c for c in store.list_corrections(path, root_run_id) if c['state'] in gated]
    raw = base64.b64decode(root['input_snapshot']['bytes_b64'], validate=True)
    csv_bytes, diffs = rules.rebuild_csv(raw, verified)
    # 以更正后的字节构建新快照：evaluate 的摘要只认 snapshot 字节；
    # 根批次的原始快照保持不变，更正前后两套字节都在台账与血缘中可追溯。
    new_snapshot = {'bytes_b64': base64.b64encode(csv_bytes).decode(),
                    'context': root['input_snapshot']['context']}
    result = evaluate({'dataset': root['source']}, snapshot=new_snapshot)
    result['lineage'] = {
        'root_run_id': root_run_id,
        'parent_run_id': store.active_run(path, root_run_id)['active_run_id'],
        'correction_ids': [c['correction_id'] for c in verified],
        'diffs': diffs,
        'rule_version': result['manifest']['rule_version']}
    return result, diffs


def verify(path, correction_id, decision=None):
    """证据闸门 + 生效。校验失败 → 已拒绝并继续等待；构建失败 → 保留旧版结果。"""
    existing = store.list_corrections(path)
    current = next((c for c in existing if c['correction_id'] == correction_id), None)
    _require(current is not None, 'CORRECTION_NOT_FOUND', '更正记录不存在', )
    _require(current['state'] != store.APPLIED, 'CORRECTION_ALREADY_VERIFIED',
             '更正已证实并已生效，重复提交不新增结果')
    _require(current['state'] != store.REJECTED, 'CORRECTION_TERMINAL',
             '更正已拒绝且为终态；补齐依据请登记新更正，原拒绝记录保留可追溯', 409)
    ok, code, detail = rules.check_evidence(current)
    if decision == 'reject' or not ok:
        reason = f'人工驳回：{current["reason"]}' if decision == 'reject' and ok else f'{code} {detail}'.strip()
        return store.set_verdict(path, correction_id, store.REJECTED, reason), None
    verified = store.set_verdict(path, correction_id, store.VERIFIED,
                                 f'证据闸门通过（{current["evidence_ref"]}）')
    root = current['root_run_id']
    try:
        result, diffs = _build_from_verified(path, root)
    except BusinessError as exc:
        # 替换失败：更正保留已证实但未产出，活跃指针不动，旧批次继续用于复检与锁库判断。
        store.mark_attempt(path, correction_id, error=f'{exc.code} {exc.message}')
        return store.get_correction_safe(path, correction_id), None
    saved = store.save(path, result)
    old_id = store.replace_active(path, root, saved['manifest']['run_id'],
                                  '已证实更正重建成功；未证实更正仍处待处理，未参与计算')
    applied = store.mark_attempt(path, correction_id, produced_run_id=saved['manifest']['run_id'])
    return applied, {'run_id': saved['manifest']['run_id'], 'previous_run_id': old_id, 'diffs': diffs}


def retry(path, correction_id):
    """对替换失败的已证实更正重试构建；同输入同规则批号不变，不新增结果。"""
    matches = [c for c in store.list_corrections(path) if c['correction_id'] == correction_id]
    _require(matches, 'CORRECTION_NOT_FOUND', '更正记录不存在')
    current = matches[0]
    _require(current['state'] == store.VERIFIED and current['last_error'],
             'CORRECTION_NOT_RETRYABLE', '仅替换失败的已证实更正可重试')
    root = current['root_run_id']
    try:
        result, diffs = _build_from_verified(path, root)
    except BusinessError as exc:
        store.mark_attempt(path, correction_id, error=f'{exc.code} {exc.message}')
        return store.get_correction_safe(path, correction_id), None
    saved = store.save(path, result)
    old_id = store.replace_active(path, root, saved['manifest']['run_id'], '重试构建成功，替换当前可用批次')
    store.mark_attempt(path, correction_id, produced_run_id=saved['manifest']['run_id'])
    return store.get_correction_safe(path, correction_id), \
        {'run_id': saved['manifest']['run_id'], 'previous_run_id': old_id, 'diffs': diffs}
