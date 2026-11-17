from fastapi import Request
from fastapi.templating import Jinja2Templates
from app.config import ROOT, META
from app.engine import evaluate
from app import store
from app.modules.correction import rules as correction_rules

templates = Jinja2Templates(directory=str(ROOT / 'app/templates'))


def render(request: Request, key):
    from app.main import DB_PATH
    run_id = request.query_params.get('run_id')
    if run_id:
        state = store.get(DB_PATH, run_id)
    else:
        state = store.save(DB_PATH, evaluate({'dataset': request.query_params.get('dataset', 'dirty')}))
    if key == 'correction':
        root = state.get('lineage', {}).get('root_run_id', state['manifest']['run_id'])
        corrections = store.list_corrections(DB_PATH, root)
        lineage = store.lineage(DB_PATH, root)
        active = store.active_run(DB_PATH, root)
        op_label = {'ADD': '新增', 'UPDATE': '变更', 'WITHDRAW': '撤回'}
        rows = [{
            'correction_id': c['correction_id'], 'op': op_label[c['op']], 'target': c['target'],
            'before': c['before_value'], 'after': '（已撤回）' if c['op'] == 'WITHDRAW'
            else c['payload'].get('value', ''),
            'state': c['state'], 'reason': c['reason'],
            'evidence_ref': c['evidence_ref'], 'attempts': c['attempts'],
            'last_error': c['last_error'] or '', 'produced_run_id': c['produced_run_id'] or '',
            'submitted_at': c['submitted_at'][:19].replace('T', ' '),
        } for c in corrections]
        lineage_rows = []
        for item in lineage['chain']:
            lin = item['lineage']
            is_active = item['run_id'] == lineage['active_run_id']
            if is_active:
                if not lin:
                    replacement = '原始批次（导入即生效，当前仍生效）'
                else:
                    replacement = '可替换：已证实更正重建成功，当前生效'
            else:
                replacement = '继续等待：已被后续批次替换为历史版本，记录保留可回溯'
            lineage_rows.append({
                'run_id': item['run_id'], 'replacement': replacement,
                'active_run_id': '是' if is_active else '否',
                'correction_ids': '、'.join(lin.get('correction_ids', [])) if lin else '—',
                'status': item['balance'].get('status', '—'),
                'generated_at': item['generated_at'][:19].replace('T', ' ')})
        state['views']['correction'] = {
            'title': '迟到更正', 'rows': rows, 'lineage_rows': lineage_rows,
            'metrics': correction_rules.summarize(corrections)}
        state['correction_board'] = {
            'root_run_id': root,
            'active_run_id': active['active_run_id'] if active else root,
            'waiting': [c['correction_id'] for c in corrections if c['state'] == store.PENDING],
            'rejected': [c['correction_id'] for c in corrections if c['state'] == store.REJECTED]}
    return templates.TemplateResponse(request=request, name=f'{key}.html',
                                      context={'meta': META, 'key': key, 'state': state,
                                               'view': state['views'][key]})
