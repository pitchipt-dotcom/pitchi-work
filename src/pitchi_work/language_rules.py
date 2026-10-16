import json,re
from pathlib import Path
METHODS=('extract_select_match','deterministic','template','structured_output',
         'constrained_generation','bonsai_freeform_ja','external_ja_fragment')

def validate_work(work):
    if not isinstance(work,dict):raise ValueError('language_work must be an object')
    feasible=work.get('methods')
    if not isinstance(feasible,list) or not feasible or any(m not in METHODS[:-1] for m in feasible):raise ValueError('language_work.methods must list feasible local methods')
    for k in ('high_quality_required','unavoidable'):
        if type(work.get(k)) is not bool:raise ValueError('language_work boolean required: '+k)
    for k in ('reason','segment'):
        if not isinstance(work.get(k),str) or not work[k].strip():raise ValueError('language_work requires '+k)
    for k in ('structured_input','output_file'):
        if k in work and (not isinstance(work[k],str) or not work[k] or Path(work[k]).is_absolute() or '..' in Path(work[k]).parts):
            raise ValueError('language_work '+k+' must be a safe relative path')
    if 'output_file' in work and Path(work['output_file']).suffix.lower() not in {'.txt','.md'}:
        raise ValueError('external output_file must be prose .txt/.md, not code or JSON')
    if work['high_quality_required'] and set(feasible)=={'bonsai_freeform_ja'} and not work['unavoidable']:
        raise ValueError('required high-quality prose with only freeform feasible must explain unavoidable=true or provide a feasible stronger method')

def language_contract_required(root):
    if not root:return False
    text=root.get('request','')+'\n'+json.dumps(root.get('requirements',{}),ensure_ascii=False)
    return bool(re.search(r'高品質|high.quality|自然.{0,20}日本語|日本語.{0,25}(?:自然|明瞭)|language_work',text,re.I))

def plan_language_errors(plan,root):
    """Do not silently lose a root language requirement during replanning."""
    if not language_contract_required(root):return []
    errors=[]
    explicit=set(re.findall(r'output_file\s*[:=]\s*[\"\']?([A-Za-z0-9_./-]+\.(?:txt|md))',root.get('request','')))
    for task in plan.get('tasks',[]):
        prose=[p for p in task.get('expected_outputs',[]) if Path(p).suffix.lower() in {'.txt','.md'}]
        work=task.get('language_work')
        if work is not None:
            try:validate_work(work)
            except (ValueError,TypeError):continue  # ordinary schema validator supplies precise errors
        if prose and work is None:
            errors.append(task['task_id']+': root requires Japanese-quality planning; prose outputs '+str(prose)+' need language_work classification. Use local/template for ordinary README; high_quality_required=true and unavoidable=true only for genuinely unavoidable prose. External prose needs structured_input and output_file.')
        elif work and set(prose)&explicit:
            if not work['high_quality_required'] or not work['unavoidable'] or work.get('output_file') not in explicit:
                errors.append(task['task_id']+': preserve explicitly requested unavoidable high-quality prose output and its language_work.output_file; do not weaken the root requirement')
            if re.search(r'methods\s*[:=]\s*\[\s*[\"\']?bonsai_freeform_ja[\"\']?\s*\]',root.get('request','')) and set(work['methods'])!={'bonsai_freeform_ja'}:
                errors.append(task['task_id']+': preserve the explicitly requested methods=[bonsai_freeform_ja] for the named unavoidable prose fragment')
        if work and work.get('high_quality_required') and work.get('unavoidable') and set(work.get('methods',[]))=={'bonsai_freeform_ja'}:
            if work.get('output_file') not in prose or work.get('structured_input') not in task.get('input_files',[])+task.get('expected_outputs',[]):
                errors.append(task['task_id']+': unavoidable external prose requires output_file in declared prose outputs and structured_input in declared inputs/outputs')
    return errors
