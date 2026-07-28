"""Boundaries and packaging invariants for what S-Agreement ships.

Source scans rather than integration tests, so each assertion runs in the
repository owning the source it guards and fails in the pull request that
breaks it. Core, S-Kanban and Personal Cockpit hold matching shares.

These matter more here than they did while S-Agreement lived inside Core.
Core's own suite scanned it as the shipped example; once it moved out, that
scan lost its subject and these took over.
"""

import ast
import importlib.metadata
import unittest
from importlib.resources import files
from pathlib import Path

import s_agreement
import sovereign


ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted((ROOT / "src").rglob("*.py"))
OTHER_APPLICATIONS = ("personal_cockpit", "s_kanban")


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }


class PackagingTests(unittest.TestCase):
    def test_distribution_and_module_versions_agree(self):
        self.assertEqual(
            importlib.metadata.version("s-agreement"), s_agreement.__version__,
        )

    def test_the_distribution_is_apache_licensed_like_every_application(self):
        # It carried Core's LGPL only because it lived inside Core's
        # repository, where NOTICE makes the repository licence the default
        # for examples. Moving out without this change would have shipped a
        # copyleft application while claiming the application licence.
        metadata = importlib.metadata.metadata("s-agreement")
        declared = metadata.get("License-Expression") or metadata.get("License") or ""
        self.assertIn("Apache-2.0", declared)

    def test_installed_browser_assets_are_available(self):
        assets = files("s_agreement.assets")
        self.assertIn(
            "<!doctype html",
            assets.joinpath("agreement.html").read_text(encoding="utf-8"),
        )
        self.assertTrue(assets.joinpath("agreement.css").is_file())

    def test_package_sources_live_under_the_declared_src_root(self):
        # Asserting where the imported module loaded from only holds for an
        # editable install: CI installs a wheel, so __file__ points into
        # site-packages. The invariant is this repository's layout.
        self.assertTrue((ROOT / "src" / "s_agreement" / "__init__.py").is_file())
        self.assertFalse((ROOT / "s_agreement").exists())


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SOURCES, "no S-Agreement sources found")

    def test_imports_core_only_through_its_public_root(self):
        public_names = set(sovereign.__all__)
        for path in SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            violations = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    violations.extend(
                        alias.name for alias in node.names
                        if alias.name == "sovereign"
                        or alias.name.startswith("sovereign.")
                    )
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module.startswith("sovereign."):
                        violations.append(module)
                    elif module == "sovereign":
                        violations.extend(
                            f"sovereign.{alias.name}"
                            for alias in node.names
                            if alias.name == "*" or alias.name not in public_names
                        )
            self.assertEqual(violations, [], str(path))

    def test_does_not_import_another_application(self):
        for path in SOURCES:
            imports = imported_modules(path)
            self.assertFalse(any(
                name == package or name.startswith(f"{package}.")
                for name in imports
                for package in OTHER_APPLICATIONS
            ), str(path))

    def test_does_not_read_private_channel_services_from_config(self):
        for path in SOURCES:
            source = path.read_text(encoding="utf-8")
            self.assertNotIn('config.get("_channel_manager")', source, str(path))
            self.assertNotIn('config.get("_relay_manager")', source, str(path))
            self.assertNotIn("channel_manager", source, str(path))

    def test_does_not_read_mutable_session_registries(self):
        forbidden = {
            "peer_topic_sets", "peer_perspectives", "peer_identity_key",
            "active_topic_uuids", "app_metadata",
        }
        for path in SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            used = {
                node.attr for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
            }
            self.assertFalse(used & forbidden, str(path))

    def test_reads_the_transition_ranking_rather_than_copying_it(self):
        # Kanban and Agreement had each copied Session's ranking and the
        # copies drifted: one ranked divergence 6, the other 5, so the same
        # conflict surfaced differently in each. Session owns the ranking.
        for path in SOURCES:
            source = path.read_text(encoding="utf-8")
            if "TRANSITION_PRIORITY" not in source:
                continue
            self.assertIn("Session.TRANSITION_PRIORITY", source, str(path))
            for literal in ('"divergence": 5', '"divergence": 6'):
                self.assertNotIn(literal, source, f"{path} re-declares the ranking")

    def test_domain_logic_does_not_depend_on_host_or_http_controllers(self):
        path = ROOT / "src" / "s_agreement" / "logic.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = imported_modules(path)
        self.assertFalse(
            any(
                name == "starlette"
                or name.startswith("starlette.")
                or name.endswith(".controller")
                or name.endswith("_controller")
                or name == "sovereign.application"
                for name in imports
            ),
            str(path),
        )
        self.assertFalse(any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in {"build_routes", "create_application"}
            for node in tree.body
        ), str(path))
        self.assertFalse(any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(arg.arg == "runtime" for arg in node.args.args)
            for node in ast.walk(tree)
        ), str(path))


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.agreement = files("s_agreement.assets").joinpath(
            "agreement.html",
        ).read_text(encoding="utf-8")

    def test_peer_only_nodes_are_presented_as_proposals(self):
        self.assertIn("payloadState.proposed_nodes", self.agreement)
        self.assertIn("Accept proposal", self.agreement)
        self.assertIn("Withdraw proposal", self.agreement)
        # "Keep mine" is the Kanban reaction, offered after a merge. An
        # agreement never merges a peer's node first, so it must not appear.
        self.assertNotIn("Keep mine", self.agreement)

    def test_topic_header_delegates_navigation_and_creation_to_the_shell(self):
        self.assertNotIn("onCreateTopic", self.agreement)
        self.assertIn("SovereignShell.setTopicSelector", self.agreement)

    def test_agenda_exposes_the_shared_move_route(self):
        self.assertIn("move: '/api/agreement/agenda/move'", self.agreement)
        self.assertIn(
            "displayedChildren(current, 'agreement_section')",
            self.agreement,
        )

    def test_assets_never_navigate_to_the_bare_root_with_a_query(self):
        # "/" serves whichever application is primary, so a root-relative link
        # lands somewhere that depends on host configuration.
        for number, line in enumerate(self.agreement.splitlines(), start=1):
            for pattern in ('href = `/?', 'href="/?', "href='/?"):
                self.assertNotIn(pattern, line, f"agreement.html:{number}")


class ThemeTests(unittest.TestCase):
    """U4 was "dark everywhere" with one documented exception: the document
    surface stayed light, reasoned as paper inside a dark frame. Using the
    desktop build showed that exception reads as a bug rather than a design,
    so it is gone - amendment recorded in DESIGN_UI_CONSISTENCY.md alongside
    U4. These hold the line against it quietly coming back.
    """

    def setUp(self):
        self.css = files("s_agreement.assets").joinpath(
            "agreement.css",
        ).read_text(encoding="utf-8")

    def test_declares_a_dark_colour_scheme(self):
        self.assertIn("color-scheme: dark", self.css)

    def test_the_document_panel_is_not_painted_light(self):
        # The exact former declarations, so a revert is caught even if it
        # arrives through a different rule. Matched as declarations rather
        # than a bare substring so the history note in this file's own
        # opening comment - which names the old colour deliberately - is
        # not itself a false positive.
        self.assertNotIn("background: #f9fafb", self.css)
        self.assertNotIn("background:#f9fafb", self.css)

    def test_text_colours_are_not_the_ones_calibrated_for_a_light_panel(self):
        # #111827 was the panel's near-black text - unreadable if the panel
        # underneath it were ever made dark again without also moving this.
        self.assertNotIn("color: #111827", self.css)
        self.assertNotIn("color:#111827", self.css)


if __name__ == "__main__":
    unittest.main()
