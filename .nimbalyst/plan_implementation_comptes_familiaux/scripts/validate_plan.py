#!/usr/bin/env python3
"""Vérifications statiques du dossier d'orchestration (pas des tests de l'application)."""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    sys.exit('PyYAML requis pour valider ce backlog : pip install pyyaml')

ROOT = Path(__file__).resolve().parent.parent
manifest = yaml.safe_load((ROOT / 'backlog.yaml').read_text(encoding='utf-8'))
taxonomy = yaml.safe_load((ROOT / 'config/categories.v1.yaml').read_text(encoding='utf-8'))

assert manifest['data_start_date'] == '2026-01-01'
assert manifest['llm_must_not_require_json_schema'] is True
assert manifest['task_count'] == len(manifest['tasks']) == 46
ids = [t['id'] for t in manifest['tasks']]
assert ids == [f'T{i:02d}' for i in range(len(ids))]
assert not [k for k,n in Counter(ids).items() if n>1]
assert list(manifest['gates']) == [f'G{i}' for i in range(12)]

known_tests = set(re.findall(r'\b[A-Z]+-\d{2}\b', (ROOT / 'docs/MATRICE_TESTS.md').read_text(encoding='utf-8')))
known_tests.update(f'LLM-{i:02d}' for i in range(1,19))

for t in manifest['tasks']:
    assert t['suggested_role'] in {'ARCH','DATA','DOMAIN','AI','ANALYTICS','UI','QA','OPS'}, t
    assert t['reasoning_level'] in {'high','xhigh'}
    assert 0 <= t['lot'] <= 11 and t['gate'] == f"G{t['lot']}"
    assert t['acceptance'] and t['allowed_paths'] and t['test_refs']
    assert all(int(dep[1:]) < int(t['id'][1:]) for dep in t['depends_on'])
    unknown = set(t['test_refs']) - known_tests
    assert not unknown, f"Tests inconnus pour {t['id']}: {unknown}"

all_category_ids = []
for family in taxonomy['expense_categories']:
    all_category_ids.append(family['id'])
    all_category_ids.extend(ch['id'] for ch in family.get('children',[]))
for group in ('income_categories','non_consumption_flows'):
    all_category_ids.extend(ch['id'] for ch in taxonomy[group])
assert len(set(all_category_ids)) == len(all_category_ids), 'Taxonomie : IDs dupliqués'

for f in ['AGENTS.md','PLAN_ORCHESTRATION.md','PROMPT_ORCHESTRATEUR.md',
          'docs/MATRICE_TESTS.md','docs/PROTOCOLE_LLM_TOLERANT.md',
          'config/categories.v1.yaml']:
    assert (ROOT / f).is_file(), f

print(f"OK dossier : {len(ids)} tickets, 12 gates, {len(all_category_ids)} IDs de catégories, {len(known_tests)} tests référencés.")
