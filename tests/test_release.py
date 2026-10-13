"""Release-contract checks: static artifacts, no production side effects."""
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReleaseContractTests(unittest.TestCase):
    def test_public_documents_do_not_embed_personal_home_paths(self):
        documents = list(ROOT.glob("*.md")) + list((ROOT / "docs").rglob("*.md"))
        for path in documents:
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertIsNone(re.search(r"/(?:root|home)/[A-Za-z0-9_.-]", path.read_text()),
                                  "Use a portable installation-root example instead of a private home path")

    def test_product_version_is_consistent_across_package_and_deployment(self):
        from douyin_monitor import __version__
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(__version__, "0.1.0")
        self.assertEqual(project["version"], __version__)
        docker = (ROOT / "packaging/docker/Dockerfile").read_text()
        self.assertIn(f"/app/releases/{__version__}/venv", docker)
        self.assertIn(f"ln -s releases/{__version__} /app/current", docker)
        self.assertIn(f"image: douyin-presence-monitor:{__version__}",
                      (ROOT / "packaging/docker/compose.yaml").read_text())
        self.assertTrue((ROOT / f"docs/validation-{__version__}.md").is_file())
        self.assertIn(f"## {__version__}", (ROOT / "CHANGELOG.md").read_text())

    def test_public_documents_and_examples_exist(self):
        for name in ("README.md", "CHANGELOG.md", "LICENSE", "SECURITY.md",
                     "docs/operations.md", "examples/config.example.json"):
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).is_file(), name)

    def test_console_entrypoint_has_no_web_runtime_dependencies(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(project["scripts"]["douyin-monitor"], "douyin_monitor.cli:main")
        self.assertEqual(project["dependencies"], [])

    def test_systemd_has_credentials_state_and_independent_logs(self):
        path = ROOT / "packaging/systemd/douyin-presence-monitor.service"
        self.assertTrue(path.is_file(), str(path))
        unit = path.read_text()
        for required in ("User=douyin-monitor", "Group=douyin-monitor", "LoadCredential=douyin_session:",
                         "LoadCredential=telegram_token:", "ReadWritePaths=@DPM_HOME@/data",
                         "@DPM_HOME@/logs", "NoNewPrivileges=yes",
                         "ProtectSystem=strict", "ProtectHome=yes", "UMask=0077"):
            self.assertIn(required, unit)
        self.assertNotIn("--token", unit)
        self.assertNotIn("--password", unit)

    def test_container_is_nonroot_without_network_listener(self):
        dockerfile = ROOT / "packaging/docker/Dockerfile"
        compose = ROOT / "packaging/docker/compose.yaml"
        self.assertTrue(dockerfile.is_file())
        self.assertTrue(compose.is_file())
        self.assertIn("USER 10001:10001", dockerfile.read_text())
        self.assertNotIn("EXPOSE", dockerfile.read_text())
        value = compose.read_text()
        self.assertNotIn("ports:", value)
        self.assertIn("read_only: true", value)
        self.assertIn("no-new-privileges:true", value)

    def test_release_includes_tests_ci_and_secret_exclusions(self):
        workflow = ROOT / ".github/workflows/ci.yml"
        self.assertTrue(workflow.is_file())
        self.assertIn("unittest discover", workflow.read_text())
        ignore = (ROOT / ".dockerignore").read_text()
        for item in (".git", "*.sqlite3*", "douyin_session", "telegram_token"):
            self.assertIn(item, ignore)


if __name__ == "__main__":
    unittest.main()
