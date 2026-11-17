from fastapi import APIRouter, Request, Body
from app.web import render
from app import store
from app.modules.correction import service, rules

router = APIRouter()


@router.get('/correction')
def page(request: Request):
    return render(request, 'correction')


@router.post('/api/correction')
def board(payload: dict = Body(default={})):
    """模块统一入口约定：返回更正看板（不接收参数，不触发计算）。"""
    from app.main import DB_PATH
    run_id = payload.get('run_id')
    if not run_id:
        return {'metrics': rules.summarize([]), 'corrections': [], 'lineage': None}
    run = store.get(DB_PATH, run_id)
    root = run.get('lineage', {}).get('root_run_id', run_id)
    corrections = store.list_corrections(DB_PATH, root)
    return {'metrics': rules.summarize(corrections), 'corrections': corrections,
            'lineage': store.lineage(DB_PATH, root)}


@router.post('/api/runs/{run_id}/corrections')
def submit(run_id: str, payload: dict = Body(...)):
    from app.main import DB_PATH
    saved, created = service.submit(DB_PATH, run_id, payload)
    return {'created': created, 'correction': saved,
            'notice': '更正已登记为待处理：证据未证实前不参与复检与锁库判断，当前批次继续生效'}


@router.post('/api/corrections/{correction_id}/verify')
def verify(correction_id: str, payload: dict = Body(default={})):
    from app.main import DB_PATH
    decision = payload.get('decision') if isinstance(payload, dict) else None
    correction, replacement = service.verify(DB_PATH, correction_id, decision)
    return {'correction': correction, 'replacement': replacement,
            'notice': ('新批次已替换当前可用批次' if replacement else
                       '未发生替换：未证实或重建失败，当前旧版结果继续生效')}


@router.post('/api/corrections/{correction_id}/retry')
def retry(correction_id: str):
    from app.main import DB_PATH
    correction, replacement = service.retry(DB_PATH, correction_id)
    return {'correction': correction, 'replacement': replacement}


@router.get('/api/runs/{run_id}/corrections')
def listing(run_id: str):
    from app.main import DB_PATH
    run = store.get(DB_PATH, run_id)
    root = run.get('lineage', {}).get('root_run_id', run_id)
    corrections = store.list_corrections(DB_PATH, root)
    return {'root_run_id': root,
            'active': store.active_run(DB_PATH, root),
            'lineage': store.lineage(DB_PATH, root),
            'corrections': corrections,
            'metrics': rules.summarize(corrections)}
