"""Model-free pipeline observations / comparisons, never relevance or completion.

Collectors supply observations. Host pins their input, producer, and data hashes.
Counts/positions are STRICT observations; clinical relevance remains ASSISTED.
"""
import hashlib
import json
from pathlib import Path
from .store import dump, load
from .runner import HostDefect
from .mission_profiles import sha, visible

def pipeline(observation):
    rows=observation.get('queries')
    if not isinstance(rows,list) or not 1<=len(rows)<=12:raise ValueError('Expected 1..12 observed queries')
    output=[]
    def count(value):
        if type(value) is not int or value<0:raise ValueError('Observed counts must be nonnegative integers')
        return value
    for row in rows:
        query=row.get('query')
        if not isinstance(query,str) or not query or len(query)>300:raise ValueError('Explicit observed query required')
        stages=[]
        for s in row.get('retrieval_stages',[]):
            stages.append({'stage':s['stage'],'candidate_count':count(s['candidate_count']),
                           'admitted_new':count(s.get('admitted_new',0)),'fts':s.get('fts'),
                           'relaxation_level':s.get('relaxation_level'),'relaxation_explicit':s.get('relaxation_level') is not None})
        sources={}
        for source,candidates in row.get('provider_candidates',{}).items():
            if not isinstance(candidates,list):raise ValueError('Source candidates must be observed arrays')
            expected=row.get('retrieval_counts',{}).get(source,len(candidates))
            if count(expected)!=len(candidates):raise ValueError('Candidate count and observed rows differ')
            def project(r):
                return {k:r[k] for k in ('id','title','name','section_title','guideline_title','source_url','canonical_url',
                    'source_level','abstract_snippet','summary','editorial_summary','search_relevance') if k in r}
            sources[source]={'observed_provider_count':expected,'candidate_view':[project(r) for r in candidates[:20]],
                'candidate_view_capped':len(candidates)>20,'display_ids':[str(r['id']) for r in row.get('provider_top',{}).get(source,[])],
                'count_scope':'provider-returned candidates; raw SQL/FTS counts are distinct stages'}
        output.append({'query':query,'normalized_query':row.get('normalized_query',query),
            'normalized_query_observed':'normalized_query' in row,
            'normalization':row.get('normalization'),'concepts':row.get('concepts',[]),
            'concept_groups':row.get('guideline_concept_groups',[]),'metadata_expansion':row.get('metadata_expansion',[]),
            'stages':stages,'sources':sources,'evidence_pool':row.get('evidence_pool'),
            'final_ranking':row.get('final_ranking',[]),'semantic_relevance':'ASSISTED, not automatically certified'})
    return {'schema':'search-pipeline-v1','queries':output,'completion_authority':False}

def compare(before,after,controls=()):
    a={q['query']:q for q in before['queries']};b={q['query']:q for q in after['queries']}
    if len(a)!=len(before['queries']) or len(b)!=len(after['queries']) or set(a)!=set(b):raise ValueError('Same unique query set required for Before/After')
    if any(q not in a for q in controls):raise ValueError('Control must be an observed query, not invented')
    result=[]
    for query,x in a.items():
        y=b[query];sources=[]
        for source in sorted(set(x['sources'])|set(y['sources'])):
            left=x['sources'].get(source);right=y['sources'].get(source)
            sources.append({'source':source,'before_count':left['observed_provider_count'] if left else None,
                'after_count':right['observed_provider_count'] if right else None,
                'before_candidates':left['candidate_view'] if left else [],
                'after_candidates':right['candidate_view'] if right else [],
                'before_view_capped':left['candidate_view_capped'] if left else False,
                'after_view_capped':right['candidate_view_capped'] if right else False,
                'before_display_ids':left['display_ids'] if left else [],'after_display_ids':right['display_ids'] if right else [],
                'route_present_both':left is not None and right is not None})
        result.append({'query':query,'control':query in controls,'sources':sources,
                       'before_ranking':x['final_ranking'],'after_ranking':y['final_ranking'],
                       'before_normalized_query':x['normalized_query'],'after_normalized_query':y['normalized_query'],
                       'normalization_observed_both':x['normalized_query_observed'] and y['normalized_query_observed'],
                       'before_concepts':x['concepts'],'after_concepts':y['concepts'],
                       'before_expansion':x['metadata_expansion'],'after_expansion':y['metadata_expansion'],
                       'normalization_changed':x['normalization']!=y['normalization'],
                       'concepts_changed':x['concepts']!=y['concepts'],
                       'before_stages':x['stages'],'after_stages':y['stages']})
    return {'schema':'search-comparison-v1','observations':result,'semantic_relevance':'ASSISTED',
            'is_validator_pass':False,'is_mission_completed':False}

def record(run,name,observation,source_reference,producer_path,data_path):
    value=pipeline(observation);source=run.path/source_reference
    if not source.resolve().is_relative_to(run.path.resolve()) or not source.is_file():raise HostDefect('Pipeline source reference missing')
    saved=load(source)
    if saved.get('observations',saved)!=observation:raise HostDefect('Pipeline observation differs from saved collector source')
    value['provenance']={'authority':'host','source_reference':source_reference,'source_sha256':sha(source),
                         'producer_sha256':sha(producer_path),'data_sha256':sha(data_path)}
    path=run.path/'artifacts'/('search_pipeline_'+name+'.json');dump(path,value)
    return {'reference':str(path.relative_to(run.path)),'sha256':sha(path)}

def compare_tool(request,*,cancelled=lambda:False):
    """Explicit local files only; no commands, services, model or data mutation."""
    try:
        inputs=[]
        for key in ('before','after'):
            original=Path(request[key]);p=original.resolve()
            if not visible(str(p)) or p.suffix!='.json' or original.is_symlink() or p.stat().st_size>10_000_000:
                raise ValueError('Explicit bounded JSON observation file required')
            if cancelled():raise ValueError('Cancelled')
            value=load(p);inputs.append((p,pipeline(value.get('observations',value))))
        result=compare(inputs[0][1],inputs[1][1],request.get('controls',[]))
        result['input_hashes']=[sha(p) for p,_ in inputs]
        return {'capability':'search.pipeline_compare','ok':True,'result':result}
    except (OSError,ValueError,KeyError,TypeError) as exc:
        return {'capability':'search.pipeline_compare','ok':False,'error':{'code':'PIPELINE_INPUT_INVALID','owner':'host','message':str(exc)}}
