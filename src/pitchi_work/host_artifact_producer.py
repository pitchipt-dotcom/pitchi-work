"""Host-owned persistence of an actual QA return, not generated test code."""
from pathlib import Path


def produce(workspace, question, qa, retrieve, save):
    result = qa.answer_question(question, retrieve)
    # Do not replace an answer or infer a missing field. Validators decide
    # whether the actual returned object satisfies the fixed criteria.
    artifact = {'question': question,
                **{key: result[key] for key in ('answer', 'insufficient', 'citations') if key in result}}
    path = Path(workspace) / 'search/qa_artifacts.json'
    save(path, artifact)
    return artifact
