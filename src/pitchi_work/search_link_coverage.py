"""Host comparisons for frozen bibliography gold; no document downloader or index."""
import json
from pathlib import Path
from .store import load
from .mission_profiles import sha, UnsupportedMission
from .runner import HostDefect

def check_gold(gold,request):
    if not isinstance(gold,dict) or gold.get('schema_version')!=1 or gold.get('frozen_before_implementation') is not True:
        raise UnsupportedMission('Frozen search gold required')
    rows=gold.get('queries',[]);selected=[q for q in rows if q.get('acceptance') is True]
    if not selected or len(rows)>12 or gold.get('required_mode')!='metadata_only':
        raise UnsupportedMission('Bounded metadata/link gold required')
    for row in selected:
        expected=row.get('expected')
        if not isinstance(expected,dict) or any(not expected.get(k) for k in ('id','title','issuer','edition','revision_year','official_url')):
            raise UnsupportedMission('Exact bibliographic expectation required')
        if row['query'] not in request or type(row.get('max_rank')) is not int or not 1<=row['max_rank']<=6:
            raise UnsupportedMission('Gold query must occur in original request with fixed top-six threshold')
    if len({q['query'] for q in rows})!=len(rows) or not isinstance(gold.get('holdout'),list) or len(gold['holdout'])>3:
        raise UnsupportedMission('Unique gold queries and finite holdout required')
    return gold

def protected_gold(run):
    binding=load(run.path/'search_binding.json')
    if sha(run.path/'search_gold.json')!=binding['gold_sha256']:raise HostDefect('Frozen gold changed')
    return load(run.path/'search_gold.json')

def canonical_metadata(run):
    """Resolve protected JSON fields independently of editable PHP scoring."""
    binding=load(run.path/'search_binding.json');workspace=Path(run.state()['workspace']);out={}
    for name,expected_hash in binding['source_hashes'].items():
        if not name.startswith('config/guideline_metadata/') or not name.endswith('.json'):continue
        if sha(workspace/name)!=expected_hash:raise HostDefect('Protected catalog changed')
        m=load(workspace/name);identifier='metadata:'+m['id']
        if identifier in out:raise HostDefect('Duplicate catalog identifier')
        out[identifier]={'id':identifier,'guideline_id':m['id'],'section_title':m['title'],
            'guideline_title':m['title'],'issuing_body':m['issuer'],'publication_year':m['revision_year'],
            'edition':m['edition'],'source_url':m['official_url'],'verified_at':m['checked_at'],
            'collection_mode':'metadata_only','summary_provenance':'bibliographic',
            'chapter':None,'page_start':None,'page_end':None,'section_url':None,
            'keywords_json':json.dumps(m['diseases']+m['synonyms']+m['search_terms'],ensure_ascii=False,separators=(',',':')),
            'tags_json':json.dumps(m['diseases'],ensure_ascii=False,separators=(',',':'))}
    return out

def validate_links(run,before,after):
    gold=protected_gold(run);canonical=canonical_metadata(run);results=[];checks=[]
    bq={q['query']:q for q in before['queries']};aq={q['query']:q for q in after['queries']}
    forbidden={'ng236-early-supported-discharge','ng236-rehab-intensity','ng185-cardiac-rehab-access',
        'ng106-cardiac-rehabilitation','ng115-pulmonary-rehabilitation','cg83-early-critical-care-rehabilitation'}
    for definition in gold['queries']:
        if not definition['acceptance']:continue
        q=definition['query'];expected=definition['expected'];identifier='metadata:'+expected['id']
        rows=aq[q]['provider_top']['guidelines'];hit=next((x for x in rows if x.get('id')==identifier),None)
        rank=next((i+1 for i,x in enumerate(rows) if x.get('id')==identifier),None)
        matched=bool(hit) and rank<=definition['max_rank'] and all(hit.get(k)==v for k,v in {
            'guideline_title':expected['title'],'issuing_body':expected['issuer'],'edition':expected['edition'],
            'publication_year':expected['revision_year'],'source_url':expected['official_url'],'collection_mode':'metadata_only'}.items())
        matched=matched and identifier in canonical and all(hit.get(k) is None for k in ('chapter','page_start','page_end','section_url'))
        ranked=any(x.get('category')=='guidelines' and x.get('id')==identifier for x in aq[q]['final_ranking'])
        noise=bool(forbidden.intersection(x.get('id') for x in rows))
        results.append({'query':q,'before_found':any(x.get('id')==identifier for x in bq[q]['provider_top']['guidelines']),
            'after_found':bool(hit),'rank':rank,'bibliography_matches':bool(matched),'final_ranked':ranked,'old_noise_present':noise})
        checks.append({'validator':'gold_link:'+definition['id'],'pass':bool(matched and ranked and not noise)})
    holdout=[]
    for q in gold['holdout']:
        signature=lambda row:{k:[x.get('id') for x in v] for k,v in row['provider_top'].items()}
        stable=signature(bq[q])==signature(aq[q])
        holdout.append({'query':q,'ids_and_counts_unchanged':stable})
        checks.append({'validator':'fixed_holdout:'+q,'pass':stable})
    return checks,{'gold_sha256':sha(run.path/'search_gold.json'),'queries':results,'holdout':holdout,
        'scope':'bibliography and official URLs only; no section/body search claim'}
