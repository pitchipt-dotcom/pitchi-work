"""Read-only search-table export helper; run on the database host.

No credential, user, event, or feedback tables are copied. Output is binary
SQLite, intended for a private artifact, never a public repository.
"""
import sqlite3
import sys

TABLES = {
    'papers', 'papers_fts', 'paper_authors', 'authors', 'sources',
    'paper_localizations', 'ontology_terms', 'ontology_aliases',
    'ontology_search_terms', 'entity_ontology', 'guidelines', 'guideline_sections',
    'guideline_sections_fts', 'guideline_recommendations', 'guideline_evidence',
    'guideline_parents', 'systems', 'system_sources', 'billing_items',
    'billing_item_documents', 'billing_requirements', 'procedure_documents',
    'document_requirements', 'support_programs', 'support_program_links',
    'support_topics', 'support_topic_requirements', 'rehab_services',
    'rehab_eligibility_rules', 'rehab_clinical_conditions', 'rehab_claim_requirements',
    'rehab_deadlines', 'rehab_service_compatibility', 'rehab_prescription_rules',
    'organization_rehab_rules', 'policy_systems', 'official_documents',
    'official_documents_fts', 'knowledge_current_overlays',
}


def export(path):
    source = sqlite3.connect('file:' + path + '?mode=ro', uri=True)
    source.execute('PRAGMA query_only=ON')
    source.execute('BEGIN')
    dest = sqlite3.connect(':memory:')
    rows = source.execute("SELECT name,sql FROM sqlite_master WHERE type='table'").fetchall()
    selected = [(name, sql) for name, sql in rows if name in TABLES]
    # Normal tables first; FTS may reference their external content.
    selected.sort(key=lambda row: 'VIRTUAL TABLE' in row[1].upper())
    # Official documents dominate disk usage. This profile does not edit their
    # provider. Keep its real retrieval candidates for target/control queries,
    # rather than duplicate all 119k documents or unrelated content.
    official_ids=set()
    for query in ('大動脈解離 リハビリテーション','脳卒中','リハビリテーション','脳卒中 歩行'):
        terms=[t for t in query.split() if len(t)>=3]
        expression=' AND '.join('"'+t.replace('"','""')+'"' for t in terms)
        official_ids.update(r[0] for r in source.execute(
            'SELECT f.rowid FROM official_documents_fts f JOIN official_documents d ON d.rowid=f.rowid '
            'WHERE official_documents_fts MATCH ? AND d.enabled=1 ORDER BY bm25(official_documents_fts,8.0,1.0,2.0,2.0,2.0),d.id LIMIT 120', (expression,)))
    for name, sql in selected:
        dest.execute(sql)
        columns = [r[1] for r in source.execute('PRAGMA table_info("' + name + '")')]
        if 'VIRTUAL TABLE' in sql.upper() or name=='official_documents':
            columns.insert(0, 'rowid')
        names = ','.join('"' + c.replace('"', '""') + '"' for c in columns)
        where=''
        if name in {'official_documents','official_documents_fts'}:
            where=' WHERE rowid IN ('+','.join(str(n) for n in sorted(official_ids))+')'
        query = source.execute('SELECT ' + names + ' FROM "' + name + '"'+where)
        while batch := query.fetchmany(1000):
            dest.executemany('INSERT INTO "' + name + '" (' + names + ') VALUES (' + ','.join('?' for _ in columns) + ')', batch)
    for name, sql in source.execute("SELECT tbl_name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"):
        if name in TABLES:
            dest.execute(sql)
    dest.commit()
    source.close()
    size = dest.execute('PRAGMA page_count').fetchone()[0] * dest.execute('PRAGMA page_size').fetchone()[0]
    if size > 300_000_000:
        raise RuntimeError('Selected search snapshot exceeds 300MB safety budget: ' + str(size))
    dest.execute('VACUUM')
    data = dest.serialize()
    if len(data) > 300_000_000:
        raise RuntimeError('Selected search snapshot exceeds 300MB safety budget')
    sys.stdout.buffer.write(data)


if __name__ == '__main__':
    export(sys.argv[1])
