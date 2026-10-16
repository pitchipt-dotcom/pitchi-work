"""Validate revisable Worker proposals against exact Host trial references.

No diagnosis, source edit, contract change, or acceptance decision is generated.
"""


def trial_summary(step,reference,receipt):
    """Do not duplicate repeated test names in every recent-history entry.

    Full test identities/errors/source provenance remain in the exact receipt;
    current test identities also remain in protected_staged_tests.
    """
    return {'step':step,'reference':reference,
            'passed':sum(r['passed'] for r in receipt['results']),
            'failed':sum(not r['passed'] for r in receipt['results'])}


def check_reconsideration(value,allowed,checks,previous,review_decision=False):
    if review_decision and set(value)=={'action','decision','test_reference'} and value['decision']=='maintain':
        # Host binds the already accepted proposal; Worker does not copy its
        # text/path identity merely to affirm that it remains the proposal.
        value={**value,'hypothesis':previous['hypothesis'],'paths':list(previous['paths'])}
    fields={'action','hypothesis','paths','test_reference'}
    if review_decision and 'decision' in value:fields.add('decision')
    if set(value)!=fields:
        raise ValueError('Reconsideration requires exact proposal and current test reference fields')
    text=value['hypothesis'];paths=value['paths']
    if not isinstance(text,str) or not text.strip() or len(text.encode())>900:
        raise ValueError('Revised hypothesis requires nonempty text <=900 UTF-8 bytes')
    decision=value.get('decision','revise')
    if review_decision:
        violations=[]
        if decision not in ('maintain','revise','reject'):
            violations.append({'field':'decision','required_action':'Choose maintain, revise or reject'})
        if decision!='maintain' and text==previous['hypothesis']:
            violations.extend([
                {'field':'decision','required_action':'If retaining this hypothesis, explicitly choose maintain'},
                {'field':'hypothesis','required_action':'If choosing revise/reject, provide a different explanation; otherwise retain it with decision maintain'}])
        if checks and value['test_reference']!=checks['reference']['reference']:
            violations.append({'field':'test_reference','required_action':'Use the current failed Host trial receipt',
                               'allowed_values':[checks['reference']['reference']]})
        if violations:
            error=ValueError('Review decision/proposal or receipt binding is inconsistent; correct only declared fields')
            error.detail={'violations':violations}
            raise error
    if decision not in ('maintain','revise','reject'):
        raise ValueError('Review decision must be maintain, revise or reject')
    if decision=='maintain' and (text!=previous['hypothesis'] or paths!=previous['paths']):
        raise ValueError('Maintain retains the exact current hypothesis and paths')
    if decision!='maintain' and text==previous['hypothesis']:
        raise ValueError('Reconsideration must not resend the unchanged hypothesis')
    if not isinstance(paths,list) or not paths or any(not isinstance(p,str) or p not in allowed for p in paths) or len(paths)!=len(set(paths)):
        raise ValueError('Revised paths must be distinct existing Host-allowed paths')
    if not checks or checks['passed'] or value['test_reference']!=checks['reference']['reference']:
        raise ValueError('Reconsideration must reference the current failed Host test receipt')
    return value
