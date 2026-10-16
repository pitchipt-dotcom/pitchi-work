"""Finite Host compilation and exact prior-PASS contract retention."""
from .host_contracts import compile_plan, errors


def compile_retaining(coarse, requirements, workspace, candidates):
    plan = compile_plan(coarse, requirements, workspace)
    formal = ('template_id','input_files','expected_outputs','allowed_paths','forbidden_paths',
              'acceptance_criteria','expected_behavior','requirement_ids','test_contract','prerequisite_tasks')
    for index, task in enumerate(plan['tasks']):
        for candidate in candidates:
            old = candidate.get('contract', {})
            if old.get('host_contract_version') == 1 and all(old.get(k) == task.get(k) for k in formal):
                plan['tasks'][index] = old
                break
    problems = errors(plan, {'requirements': requirements})
    if problems: raise ValueError('; '.join(problems))
    return plan
