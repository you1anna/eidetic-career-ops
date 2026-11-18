"""Guard record dependencies and their transitive imports, including function-local imports."""
import ast
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Each module may import only modules earlier in this list. records.py is the facade over all of them.
LAYERS = ['record_read', 'decision_views', 'incoming_views', 'record_views', 'dashboard_views', 'record_core',
          'record_identity', 'record_mutations', 'record_evidence', 'record_decisions',
          'record_review', 'source_coverage', 'workspace_state', 'state_export', 'record_outcomes', 'record_week',
          'record_export', 'incoming']


def module_imports(name, *, compatibility=False):
    """All static imports, including function-local and conditional imports."""
    path = HERE / f'{name}.py'
    if not path.exists():
        path = HERE.parent / 'tools' / f'{name}.py'
    tree = ast.parse(path.read_text())
    # states.transition is a retained legacy API forwarding to the canonical revision-safe writer.
    # It is not used by state readers. Waive this one edge, not all late imports or all of states.
    legacy = {id(n) for fn in tree.body if name == 'states' and isinstance(fn, ast.FunctionDef)
              and fn.name == 'transition' for n in ast.walk(fn)
              if isinstance(n, ast.Import) and [a.name for a in n.names] == ['records']} if compatibility else set()
    found = set()
    for node in ast.walk(tree):
        if id(node) in legacy:
            continue
        if isinstance(node, ast.Import):
            found |= {a.name.split('.')[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split('.')[0])
    return found


class RecordLayerTests(unittest.TestCase):
    def test_collection_readers_cannot_import_dispatch_or_record_mutations(self):
        forbidden={'collection_budget','collection_schedule','web_read','records','record_imports','record_mutations'}
        for name in ('collection_policy','collection_reads'):
            self.assertFalse(module_imports(name)&forbidden,name)

    def test_approach_history_is_pure_and_legacy_constants_share_one_owner(self):
        self.assertLessEqual(module_imports('approach_history'), {'collections'},
                             'historical rules must not depend on I/O or application orchestration')
        import approach_history
        import approaches
        self.assertIs(approaches.STAGE, approach_history.STAGE)
        self.assertIs(approaches.RECOMMENDATION, approach_history.RECOMMENDATION)

    def test_transitive_service_dependencies_have_no_unwaived_cycles(self):
        local = {p.stem for folder in (HERE, HERE.parent / 'tools') for p in folder.glob('*.py')}
        visited, active = set(), []

        def visit(name):
            self.assertNotIn(name, active, ' -> '.join([*active, name]))
            if name in visited:
                return
            active.append(name)
            for dependency in sorted(module_imports(name, compatibility=True) & local):
                visit(dependency)
            active.pop()
            visited.add(name)

        for name in LAYERS:
            visit(name)

    def test_service_modules_import_only_lower_layers(self):
        for n, name in enumerate(LAYERS):
            with self.subTest(module=name):
                imported = module_imports(name)
                self.assertNotIn('records', imported, 'domain modules use each other, not the facade')
                self.assertNotIn('record_imports', imported, 'shared rules cannot depend on import orchestration')
                upward = imported & set(LAYERS[n:])
                self.assertEqual(upward, set(), f'{name} imports a module at or above its layer')

    def test_the_facade_exposes_each_module_public_api(self):
        import records
        for name in (n for n in LAYERS if n not in {'record_read', 'record_views', 'dashboard_views', 'incoming_views', 'incoming'}):
            module = __import__(name)
            public = {k for k, v in vars(module).items() if not k.startswith('_')
                      and getattr(v, '__module__', name) == name and (callable(v) or k.isupper())}
            with self.subTest(module=name):
                self.assertEqual({k for k in public if not hasattr(records, k)}, set())
                for key in public:
                    self.assertIs(getattr(records, key), getattr(module, key),
                                  f'{key} must be the same implementation through the facade')

    def test_existing_projection_and_import_entry_points_share_the_implementation(self):
        import incoming
        import incoming_views
        import record_identity
        import record_imports
        import record_read
        import record_views
        for name in ('upsert_record', 'ensure_organisation', 'link_organisation'):
            self.assertIs(getattr(record_imports, name), getattr(record_identity, name))
        for name in ('snapshot', 'record_policy', 'record_exclusions'):
            self.assertIs(getattr(record_views, name), getattr(record_read, name))
        self.assertIs(incoming.attach, incoming_views.attach)


if __name__ == '__main__':
    unittest.main()
