<?php
// Host-owned, read-only observations. Not Worker-authored acceptance code.
declare(strict_types=1);
require '/tmp/project/src/bootstrap.php';
foreach (['SearchRouter','GuidelineSectionSearch','SystemProcedureSearch','LifeSupportSearch',
 'RehabEligibilitySearch','RehabPrescriptionSearch','ClinicalQueryNormalizer','EvidenceSearch',
 'AiProcessingQueue','StructuredQueryLexicon','SearchResultRanking','OfficialDocumentSearch'] as $class) {
 require_once '/tmp/project/src/'.$class.'.php';
}
$db=new PDO('sqlite:/tmp/evidence.sqlite',null,null,[PDO::ATTR_ERRMODE=>PDO::ERRMODE_EXCEPTION,PDO::ATTR_DEFAULT_FETCH_MODE=>PDO::FETCH_ASSOC]);
$db->exec('PRAGMA query_only=ON');
$queries=json_decode(file_get_contents('/tmp/queries.json'),true,512,JSON_THROW_ON_ERROR);
$tables=$db->query("SELECT name FROM sqlite_master WHERE type='table'")->fetchAll(PDO::FETCH_COLUMN);
$output=['tables'=>$tables,'queries'=>[]];
foreach($queries as $query) {
 $normal=ClinicalQueryNormalizer::resolve($db,$query);
 $concepts=ClinicalQueryNormalizer::evidenceConcepts($query,$normal,true);
 $phrases=StructuredQueryLexicon::phrases($db,$query);
 $rankingConcepts=StructuredQueryLexicon::concepts($query,$concepts,$phrases);
 $routing=SearchRouter::classify($query);
 $guidelineConcepts=(new ReflectionMethod(GuidelineSectionSearch::class,'queryConcepts'))->invoke(null,$query);
 $evidence=EvidenceSearch::search($db,$query,$normal,'relevance',1900,2100,'',50,0);
 $stageDefinitions=ClinicalQueryNormalizer::evidenceStages($concepts);
 foreach($evidence['stages'] as &$stage)foreach($stageDefinitions as $definition)if($stage['stage']===$definition['name'])$stage['relaxation_level']=$definition['relaxation_level']??null;
 unset($stage);
 $groups=['papers'=>$evidence['papers'], 'guidelines'=>GuidelineSectionSearch::search($db,$query.' '.implode(' ',$phrases),40)];
 $structuredQuery=trim($query.' '.implode(' ',$phrases));
 $structured=SystemProcedureSearch::search($db,$structuredQuery,'',40);
 $groups+=['billing_items'=>$structured['billing_items'],'documents'=>$structured['documents'],
   'life_support'=>LifeSupportSearch::search($db,$structuredQuery,40),
   'rehab_eligibility'=>RehabEligibilitySearch::search($db,$structuredQuery,40),
   'rehab_prescription'=>RehabPrescriptionSearch::search($db,$structuredQuery,40)];
 $groups['official_documents']=OfficialDocumentSearch::search($db,$query,40);
 $candidateGroups=$groups;
 $short=SearchResultRanking::shortlist($routing,$groups,['guidelines'=>6,'billing_items'=>8,'documents'=>8,
  'life_support'=>8,'rehab_eligibility'=>8,'rehab_prescription'=>6],$query,$rankingConcepts);
 $ranking=SearchResultRanking::build($routing,$short,30,$query,$rankingConcepts);
 $sanitize=static function($groups):array {
  $keep=array_flip(['id','title','name','section_title','guideline_title','source_url','canonical_url',
    'source_level','issuer','prefecture','municipality','domain','abstract_snippet','summary',
    'clinical_question','population','intervention','outcome','keywords_json','tags_json',
    'editorial_summary','official_basis','source_title','matched_terms','strong_matched_terms',
    'matched_fields','matched_concept_count','query_concept_count','relevance_score','search_relevance',
    'issuing_body','edition','publication_year','collection_mode','chapter','page_start','page_end','section_url','summary_provenance']);
  foreach($groups as &$rows)foreach($rows as &$row)$row=array_intersect_key($row,$keep);
  unset($rows,$row);return $groups;
 };
 $output['queries'][]=['query'=>$query,'normalized_query'=>ptot_normalize_query($query),'normalization'=>$normal,'concepts'=>$concepts,
  'evidence_pool'=>array_intersect_key($evidence,array_flip(['total','raw_candidate_total','candidate_pool_size','candidate_pool_capped'])),
  'metadata_expansion'=>$phrases,'retrieval_stages'=>$evidence['stages'],
  'guideline_concept_groups'=>$guidelineConcepts,
  'retrieval_counts'=>array_map('count',$candidateGroups),'provider_candidates'=>$sanitize($candidateGroups),
  'provider_top'=>$sanitize($short),'final_ranking'=>$ranking,
  'route_scope'=>'Direct real providers and existing API shortlisting/ranking; official_documents collected separately (not a ranked category)',
  'official_available'=>in_array('official_documents',$tables,true)];
}
echo json_encode($output,JSON_THROW_ON_ERROR|JSON_UNESCAPED_UNICODE|JSON_INVALID_UTF8_SUBSTITUTE);
