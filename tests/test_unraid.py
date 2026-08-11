import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "unraid_adapter", ROOT / "custom_components" / "ingress" / "unraid.py"
)
assert spec and spec.loader
unraid = importlib.util.module_from_spec(spec)
spec.loader.exec_module(unraid)
add_unraid_cookies = unraid.add_unraid_cookies
is_unraid_login_redirect = unraid.is_unraid_login_redirect


class UnraidAdapterTest(unittest.TestCase):
    def test_private_cookie_replaces_browser_cookie(self):
        cfg = SimpleNamespace(adapter_cookies={"PHPSESSID": "private"})
        headers = {"Cookie": "theme=dark; PHPSESSID=browser"}
        add_unraid_cookies(headers, cfg)
        self.assertEqual(headers["Cookie"], "theme=dark; PHPSESSID=private")

    def test_login_redirect_detection(self):
        self.assertTrue(is_unraid_login_redirect(302, "/login"))
        self.assertTrue(is_unraid_login_redirect(303, "http://tower/login"))
        self.assertFalse(is_unraid_login_redirect(302, "/Main"))
        self.assertFalse(is_unraid_login_redirect(200, "/login"))


if __name__ == "__main__":
    unittest.main()
