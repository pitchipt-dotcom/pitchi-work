"""Deterministic trial comparison, not diagnosis or acceptance."""
import hashlib
import json


def review_instructions():
    return ('Compare trial_result receipts. reconsider: maintain returns only action, decision, test_reference; '
            'Host retains proposal. revise/reject adds new hypothesis and paths. '
            'One operation; acceptance unchanged.')


class TrialProgress:
    def __init__(self, baseline, force_after=4):
        if force_after not in (2, 4):
            raise ValueError('Finite generic review thresholds are 2 or 4')
        self.force_after = force_after
        self.previous = baseline
        self.streak = 0
        self.edits = set()
        self.comparison = {}

    def observe(self, receipt, edit):
        old = {r['test']: r for r in self.previous['results']}
        new = {r['test']: r for r in receipt['results']}
        if set(old) != set(new):
            raise ValueError('Protected test identities changed between trials')
        improved = sorted(k for k in new if new[k]['passed'] and not old[k]['passed'])
        regressed = sorted(k for k in new if old[k]['passed'] and not new[k]['passed'])
        def diagnostic(row):
            execution=row.get('execution',{})
            return tuple(execution.get(k) for k in ('exit_code','stderr','stdout'))
        changed = sorted(k for k in new if not old[k]['passed'] and not new[k]['passed']
                         and diagnostic(old[k]) != diagnostic(new[k]))
        patch = {k: v for k, v in edit.items() if k not in ('summary', 'source_sha256')}
        signature = hashlib.sha256(json.dumps(patch, sort_keys=True).encode()).hexdigest()
        repeated = signature in self.edits
        self.edits.add(signature)
        # New passing identities without losing others are progress. Changed error
        # text alone is observable novelty, not proof that a candidate improved.
        self.streak = 0 if improved and not regressed else self.streak + 1
        self.previous = receipt
        self.comparison = {'improved_ids': improved, 'regressed_ids': regressed,
                           'changed_failure_ids': changed, 'repeated_edit': repeated}

    def review(self):
        self.streak = 0

    def packet(self):
        # Full test IDs/execution remain in hashed trial receipts; counts avoid
        # multiplying long identities across the working context.
        return {'required': self.streak >= self.force_after,
                'warning': self.streak >= 2, 'non_improving_trials': self.streak,
                'warning_after': 2, 'force_after': self.force_after,
                'comparison_counts': {k: len(self.comparison.get(k, [])) for k in
                                      ('improved_ids', 'regressed_ids', 'changed_failure_ids')},
                'repeated_edit': self.comparison.get('repeated_edit', False),
                'decisions': ['maintain', 'revise', 'reject']}
