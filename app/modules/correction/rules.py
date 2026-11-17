import csv, io, hashlib
from decimal import Decimal, InvalidOperation
from app.errors import BusinessError
from app.coerce import number

FIELDS = ['id', 'subject', 'visit', 'at', 'enrolled', 'kind', 'parent', 'value', 'unit', 'protocol', 'revision']
OPERATIONS = {'ADD', 'UPDATE', 'WITHDRAW'}


def sha256_text(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _norm_number(text):
    """数值归一：去千分位后按 Decimal 比较，1.20 与 1.2 视为同一数值。"""
    try:
        return number(text)
    except BusinessError:
        return None


def _evidence_matches(evidence_text, target, new_value):
    """证据可追溯规则（全部显式、可复核）：
    1) 证据文本必须包含目标记录号；
    2) 非撤回更正，证据中必须出现与更正后取值相等的数值（容差为零的 Decimal 比较）。
    撤回无需新取值，只要求记录号可追溯。"""
    if not evidence_text or target not in evidence_text:
        return False, 'EVIDENCE_TRACE_MISSING', '证据文本无法追溯到目标记录号'
    if new_value is None:
        return True, '', ''
    expected = _norm_number(new_value)
    if expected is None:
        return False, 'EVIDENCE_VALUE_BAD', '更正后取值不是有效数值'
    for token in evidence_text.replace(',', ' ').split():
        token = token.strip('：:;；，。.()（）[]【】')
        try:
            if Decimal(token) == expected:
                return True, '', ''
        except InvalidOperation:
            continue
    return False, 'EVIDENCE_VALUE_UNPROVEN', '证据文本中找不到与更正后取值一致的原始数值'


def check_evidence(correction):
    """证据闸门：哈希自洽 + 文档号 + 可追溯。任一不过即拒绝，未证实数值不得进入批次。"""
    if not correction['evidence_ref'] or not correction['evidence_ref'].strip():
        return False, 'EVIDENCE_REF_MISSING', '缺少证据文档号'
    actual = sha256_text(correction['evidence_text'])
    if actual != correction['evidence_hash']:
        return False, 'EVIDENCE_HASH_MISMATCH', '证据哈希与原文不一致，证据可能被篡改'
    new_value = None if correction['op'] == 'WITHDRAW' else correction['payload'].get('value')
    ok, code, detail = _evidence_matches(correction['evidence_text'], correction['target'], new_value)
    return ok, code, detail


def _decode(raw):
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise BusinessError('BAD_ENCODING', '源快照编码无法识别')


def _rows_from_snapshot(raw):
    text, _ = _decode(raw)
    stream = csv.reader(io.StringIO(text, newline=''), strict=True)
    header = next(stream)
    rows = []
    for cells in stream:
        if not cells or not any(v.strip() for v in cells):
            continue
        if cells == header:
            continue
        rows.append(dict(zip(header, cells)))
    return header, rows


def rebuild_csv(snapshot_raw, corrections):
    """把已证实更正按登记顺序作用于不可变源快照，输出新 CSV 字节。
    更正前后值均在此对账；任何冲突都显式报错，不静默覆盖。"""
    header, rows = _rows_from_snapshot(snapshot_raw)
    if header != FIELDS:
        raise BusinessError('SNAPSHOT_HEADER', '源快照表头与现行字段不一致，无法重建')
    by_id = {r['id']: r for r in rows}
    diffs = []
    for c in corrections:
        op, target, payload = c['op'], c['target'], c['payload']
        if op == 'ADD':
            if target in by_id:
                raise BusinessError('CORRECTION_TARGET_EXISTS', f'新增目标 {target} 已存在，不能重复新增')
            bad = set(payload) - set(FIELDS)
            if bad:
                raise BusinessError('CORRECTION_PAYLOAD_FIELD', f'新增载荷含未知字段：{sorted(bad)}')
            row = {k: '' for k in FIELDS}
            row.update(payload)
            row['id'] = target
            missing = [k for k in FIELDS if k in ('subject', 'visit', 'at', 'enrolled', 'kind', 'revision') and not str(row.get(k, '')).strip()]
            if missing:
                raise BusinessError('CORRECTION_PAYLOAD_INCOMPLETE', f'新增记录缺少必填字段：{missing}')
            rows.append(row)
            by_id[target] = row
            diffs.append({'op': op, 'target': target, 'before': '', 'after': row.get('value', '')})
        elif op == 'UPDATE':
            current = by_id.get(target)
            if current is None:
                raise BusinessError('CORRECTION_TARGET_MISSING', f'变更目标 {target} 不在当前批次中')
            bad = set(payload) - set(FIELDS)
            if bad or 'id' in payload:
                raise BusinessError('CORRECTION_PAYLOAD_FIELD', '变更只允许覆盖业务字段，不能改记录号')
            before = {k: current.get(k, '') for k in payload}
            for k, v in payload.items():
                current[k] = v
            diffs.append({'op': op, 'target': target,
                          'before': jsonish(before), 'after': jsonish(payload)})
        elif op == 'WITHDRAW':
            current = by_id.get(target)
            if current is None:
                raise BusinessError('CORRECTION_TARGET_MISSING', f'撤回目标 {target} 不在当前批次中')
            rows = [r for r in rows if r['id'] != target]
            by_id.pop(target)
            diffs.append({'op': op, 'target': target,
                          'before': current.get('value', ''), 'after': '（已撤回）'})
        else:
            raise BusinessError('CORRECTION_OP', f'不支持的更正动作：{op}')
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, lineterminator='\n')
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, '') for k in FIELDS})
    return buffer.getvalue().encode('utf-8'), diffs


def jsonish(value):
    if isinstance(value, dict):
        return '；'.join(f'{k}={v}' for k, v in value.items())
    return str(value)


def summarize(corrections):
    """页面分类计数：新增、变更、撤回、待处理、已证实、已生效、已拒绝。"""
    metrics = {'新增': 0, '变更': 0, '撤回': 0, '待处理': 0, '已证实': 0, '已生效': 0, '已拒绝': 0}
    op_label = {'ADD': '新增', 'UPDATE': '变更', 'WITHDRAW': '撤回'}
    for c in corrections:
        metrics[op_label[c['op']]] += 1
        metrics[c['state']] += 1
    return metrics


def apply(state):
    """引擎阶段只提供空骨架；更正台账在 SQLite，由 handler 注入实际数据。"""
    state['views']['correction'] = {
        'title': '迟到更正', 'rows': [], 'lineage_rows': [],
        'metrics': {'新增': 0, '变更': 0, '撤回': 0, '待处理': 0,
                    '已证实': 0, '已生效': 0, '已拒绝': 0}}
