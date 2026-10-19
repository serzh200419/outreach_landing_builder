import base64
import contextlib
import csv
import io
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import main


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def csv(self, rows, fields=None):
        fields = fields or list(dict.fromkeys(key for row in rows for key in row))
        with (self.base / "input.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def run_generator(self, ids, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return main.generate("input.csv", "output", ids, base=self.base, **kwargs)

    def overrides(self, data):
        (self.base / "personalization.json").write_text(json.dumps(data), encoding="utf-8")

    def test_bom_armenian_and_exact_selection(self):
        self.csv([{"business_name": "Լուսե", "place_id": "one"}, {"business_name": "Լուսե", "place_id": "two"}])
        result = self.run_generator(["one", "one"])
        self.assertEqual(len(result["generated"]), 1)
        self.assertIn("Լուսե", result["generated"][0].read_text(encoding="utf-8"))
        self.assertEqual(len(self.run_generator(["Լուսե"])["failed"]), 1)
        self.assertEqual(len(self.run_generator([" one"])["failed"]), 1)

    def test_empty_selection_does_not_read_or_write(self):
        self.assertEqual(self.run_generator([]), {"generated": [], "skipped": [], "failed": []})
        self.assertFalse((self.base / "output").exists())

    def test_required_columns_and_values(self):
        self.csv([{"business_name": "Name"}])
        self.assertIn("Missing required CSV columns", self.run_generator(["one"])["failed"][0][1])
        self.csv([{"business_name": " ", "place_id": "one"}])
        self.assertIn("nonempty", self.run_generator(["one"])["failed"][0][1])

    def test_duplicate_conflicts_and_identical_duplicates(self):
        row = {"business_name": "Name", "place_id": "one"}
        self.csv([row, row])
        self.assertEqual(len(self.run_generator(["one"])["generated"]), 1)
        self.csv([row, dict(row, business_name="Different")])
        self.assertIn("Conflicting", self.run_generator(["one"])["failed"][0][1])

    def test_escaping_and_optional_services(self):
        attack = '<script>alert("x")</script>'
        self.csv([{"business_name": attack, "place_id": "one", "address": attack}])
        self.overrides({"one": {"headline": attack, "services": [{"title": attack, "description": attack}]}})
        html = self.run_generator(["one"])["generated"][0].read_text(encoding="utf-8")
        self.assertNotIn(attack, html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn('id="services"', html)

    def test_unsafe_links_and_colors(self):
        for value in ["javascript:alert(1)", "data:text/html,hello", "//example.com", "https://example.com\n", "https://a\\b", "https://a:bad"]:
            self.assertIsNone(main.safe_url(value))
        self.assertEqual(main.safe_url("https://example.com/a?b=1&c=2"), "https://example.com/a?b=1&c=2")
        self.csv([{"business_name": "Name", "place_id": "one", "instagram_url": "javascript:alert(1)"}])
        self.overrides({"one": {"accent_color": "red; background:url(https://example.com)"}})
        self.assertIn("hex color", self.run_generator(["one"])["failed"][0][1])
        self.overrides({})
        html = self.run_generator(["one"])["generated"][0].read_text(encoding="utf-8")
        self.assertNotIn("javascript:", html)

    def test_safe_distinct_directories(self):
        names = [main.output_name("../../CON:*?", value) for value in ("../../one", "../../two")]
        self.assertNotEqual(*names)
        for name in names:
            self.assertRegex(name, r"^[a-z0-9-]+$")
            self.assertEqual(Path(name).name, name)
        self.assertEqual(main.output_name("Հայ", "one"), main.output_name("Հայ", "one"))

    def test_missing_optional_values(self):
        self.csv([{"business_name": "Name", "place_id": "one"}])
        html = self.run_generator(["one"])["generated"][0].read_text(encoding="utf-8")
        self.assertNotIn('href="tel:', html)
        self.assertNotIn('class="button mobile-call"', html)
        self.assertNotIn('id="services"', html)
        self.assertNotIn("<img", html)

    def test_overwrite_protection(self):
        self.csv([{"business_name": "Name", "place_id": "one"}])
        path = self.run_generator(["one"])["generated"][0]
        path.write_text("keep", encoding="utf-8")
        self.assertEqual(self.run_generator(["one"])["skipped"], [path])
        self.assertEqual(path.read_text(), "keep")
        self.run_generator(["one"], overwrite_existing=True)
        self.assertIn("<!doctype html>", path.read_text(encoding="utf-8"))
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_phone_escape_and_normalization(self):
        self.assertEqual(main.phone_values("'+374 (00) 000-001"), ("+374 (00) 000-001", "tel:+37400000001"))
        for invalid in ["''+37400000001", "javascript:1", "123", "++37400000001"]:
            self.assertIsNone(main.phone_values(invalid)[1])

    def test_invalid_override_does_not_stop_other_business(self):
        self.csv([{"business_name": "Name", "place_id": value} for value in ("one", "two")])
        self.overrides({"one": {"hero_image_path": "missing.png"}})
        result = self.run_generator(["one", "two"])
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(len(result["generated"]), 1)
        self.assertIn("Image not found", result["failed"][0][1])

    def test_raster_embedding_and_bounds(self):
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
        (self.base / "image.png").write_bytes(png)
        self.assertTrue(main.embedded_image("image.png", self.base).startswith("data:image/png;base64,"))
        (self.base / "bad.svg").write_text('<svg onload="alert(1)"/>')
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            main.embedded_image("bad.svg", self.base)
        (self.base / "big.png").write_bytes(b"x" * (main.MAX_IMAGE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "4 MiB"):
            main.embedded_image("big.png", self.base)

    def test_limit_and_malformed_json(self):
        self.csv([{"business_name": "Name", "place_id": value} for value in ("one", "two")])
        self.assertEqual(len(self.run_generator(["one", "two"], max_previews=1)["generated"]), 1)
        (self.base / "personalization.json").write_text("{")
        self.assertEqual(len(self.run_generator(["one", "two"])["failed"]), 2)

    def test_new_personalization_overrides_are_validated_and_escaped(self):
        row = {"business_name": "Name", "place_id": "one", "category": "raw_google_category"}
        for position in ("center", "top", "bottom"):
            context = main.template_context(row, {"hero_image_position": position})
            self.assertEqual(context["image_position"], position)
        for position in ("left", "center; color:red", None, []):
            with self.assertRaisesRegex(ValueError, "hero_image_position"):
                main.template_context(row, {"hero_image_position": position})
        for key in ("business_type_label", "image_caption"):
            with self.assertRaisesRegex(ValueError, key):
                main.template_context(row, {key: 123})
        self.csv([row])
        self.overrides({"one": {"business_type_label": "<b>Untrusted</b>", "accent_color": "#fff"}})
        html = self.run_generator(["one"])["generated"][0].read_text(encoding="utf-8")
        self.assertIn("&lt;b&gt;Untrusted&lt;/b&gt;", html)
        self.assertNotIn("raw_google_category", html)

    def test_screenshot_failure_preserves_successful_html(self):
        self.csv([{"business_name": "Name", "place_id": "one"}])
        with patch("screenshot.capture_batch", return_value={"captured": [], "failed": [("preview", "Chromium missing")]}) as capture:
            result = self.run_generator(["one"], generate_screenshots=True)
        capture.assert_called_once_with(result["generated"])
        self.assertTrue(result["generated"][0].is_file())
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["screenshots"]["failed"][0][1], "Chromium missing")

    def test_html_generation_does_not_call_optional_capture(self):
        self.csv([{"business_name": "Name", "place_id": "one"}])
        with patch("screenshot.capture_batch", side_effect=AssertionError("Must remain optional")):
            self.assertEqual(len(self.run_generator(["one"])["generated"]), 1)

    def test_missing_playwright_reports_installation_hint(self):
        import screenshot
        with patch.dict("sys.modules", {"playwright.sync_api": None}):
            result = screenshot.capture_batch([self.base / "index.html"])
        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("python -m pip install playwright", result["failed"][0][1])
        self.assertEqual(screenshot.capture_batch([]), {"captured": [], "failed": []})

    def test_browser_startup_failure_is_separate(self):
        import screenshot
        import types
        fake_module = types.SimpleNamespace(sync_playwright=lambda: (_ for _ in ()).throw(RuntimeError("No Chromium")))
        with patch.dict("sys.modules", {"playwright.sync_api": fake_module}):
            result = screenshot.capture_batch([self.base / "one.html", self.base / "two.html"])
        self.assertEqual(len(result["failed"]), 2)
        self.assertEqual(result["captured"], [])
        self.assertIn("No Chromium", result["failed"][0][1])

    def test_brand_title_and_description_precedence(self):
        row = {"business_name": "Original listing", "place_id": "one"}
        original = row.copy()
        context = main.template_context(row, {})
        self.assertEqual(context["brand_name"], row["business_name"])
        self.assertNotEqual(context["hero_title"], row["business_name"])
        legacy = main.template_context(row, {"display_name": "Legacy name", "headline": "Legacy tagline", "introduction": "Legacy about"})
        self.assertEqual(legacy["brand_name"], "Legacy name")
        self.assertEqual(legacy["hero_description"], "Legacy tagline")
        self.assertEqual(legacy["introduction"], "Legacy about")
        context = main.template_context(row, {"brand_name": "Short brand", "display_name": "Old name", "hero_title": "New title", "hero_description": "New description", "headline": "Old tagline"})
        self.assertEqual(context["brand_name"], "Short brand")
        self.assertEqual(context["hero_title"], "New title")
        self.assertEqual(context["hero_description"], "New description")
        self.assertEqual(context["business_name"], row["business_name"])
        self.assertEqual(row, original)

    def test_explicit_niche_and_clear_fallback(self):
        row = {"business_name": "Transport taxi route", "place_id": "one"}
        self.assertEqual(main.template_context(row, {})["niche"], "general_service")
        self.assertEqual(main.template_context(row, {}, niche="passenger_transport")["niche"], "passenger_transport")
        self.assertEqual(main.template_context(row, {"niche": "general_service"}, niche="passenger_transport")["niche"], "general_service")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            context = main.template_context(row, {"niche": "unknown"})
        self.assertEqual(context["niche"], "general_service")
        self.assertIn("falling back to general_service", output.getvalue())
        self.assertNotIn("հայեցակարգ", context["hero_description"])
        self.assertNotIn("հայեցակարգ", context["introduction"])

    def test_routes_and_services_require_explicit_data(self):
        row = {"business_name": "Երևան — Թբիլիսի", "place_id": "one"}
        context = main.template_context(row, {}, niche="passenger_transport")
        self.assertIsNone(context["route"])
        self.assertEqual(context["services"], [])
        self.assertNotIn("route", dict(context["sections"]))
        self.assertNotIn("services", dict(context["sections"]))
        self.assertNotIn("process", dict(context["sections"]))
        route = {"from": "Երևան", "to": "Թբիլիսի"}
        self.assertIsNone(main.template_context(row, {"route": route})["route"])
        context = main.template_context(row, {"route": route, "niche": "passenger_transport"})
        self.assertEqual(context["route"], route)
        self.assertIn("route", dict(context["sections"]))
        for route in ({"from": "Երևան"}, {"from": "", "to": "Թբիլիսի"}, "Երևան"):
            with self.assertRaisesRegex(ValueError, "route must contain"):
                main.template_context(row, {"route": route}, niche="passenger_transport")

    def test_navigation_targets_exactly_match_rendered_sections(self):
        class NavigationParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.ids, self.sections, self.links = [], [], []
                self.in_nav = False

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if "id" in attrs:
                    self.ids.append(attrs["id"])
                if tag == "nav":
                    self.in_nav = True
                if tag == "a" and self.in_nav:
                    self.links.append(attrs["href"])
                if tag == "section" and "id" in attrs:
                    self.sections.append("#" + attrs["id"])

            def handle_endtag(self, tag):
                if tag == "nav":
                    self.in_nav = False

        self.csv([{"business_name": "Name", "place_id": "one", "phone_number": "+37400000001"}])
        for overrides in ({}, {"faq": []}, {"niche": "passenger_transport", "route": {"from": "Երևան", "to": "Թբիլիսի"}, "services": [{"title": "Supplied", "description": "Explicit"}]}):
            self.overrides({"one": overrides})
            path = self.run_generator(["one"], overwrite_existing=True)["generated"][0]
            parser = NavigationParser()
            parser.feed(path.read_text(encoding="utf-8"))
            self.assertEqual(parser.links, parser.sections)
            self.assertEqual(len(parser.ids), len(set(parser.ids)))

    def test_brand_route_and_faq_are_escaped(self):
        attack = '<img src=x onerror="alert(1)">'
        self.csv([{"business_name": "Original", "place_id": "one"}])
        self.overrides({"one": {"brand_name": attack, "hero_title": attack, "hero_description": attack,
                                "niche": "passenger_transport", "route": {"from": attack, "to": "Destination"},
                                "faq": [{"question": attack, "answer": attack}]}})
        html = self.run_generator(["one"])["generated"][0].read_text(encoding="utf-8")
        self.assertNotIn(attack, html)
        self.assertIn("&lt;img src=x onerror=&#34;alert(1)&#34;&gt;", html)
        with self.assertRaisesRegex(ValueError, "faq must"):
            main.template_context({"business_name": "Name"}, {"faq": [{"question": "Missing answer"}]})

    def test_separate_example_personalization_preserves_customer_file(self):
        self.csv([{"business_name": "Name", "place_id": "one"}])
        self.overrides({"one": {"brand_name": "Customer brand"}})
        customer = (self.base / "personalization.json").read_bytes()
        (self.base / "demo.json").write_text(json.dumps({"one": {"brand_name": "Example brand", "niche": "passenger_transport"}}))
        path = self.run_generator(["one"], personalization_file="demo.json")["generated"][0]
        self.assertIn("Example brand", path.read_text(encoding="utf-8"))
        self.assertNotIn("Customer brand", path.read_text(encoding="utf-8"))
        self.assertEqual(customer, (self.base / "personalization.json").read_bytes())

    def test_detailing_defaults_and_conditional_services(self):
        context = main.template_context({"business_name": "Example"}, {}, niche="detailing_cleaning")
        self.assertEqual(context["hero_title"], "Մաքրություն՝ մինչև վերջին դետալը։")
        self.assertEqual(context["hero_description"], "Ծանոթացեք մեքենայի խնամքի հնարավորություններին և կապվեք՝ ծառայության մանրամասներն ու արժեքը ճշտելու համար։")
        self.assertEqual(context["accent"], "#aecbdc")
        self.assertEqual(len(context["faq"]), 3)
        self.assertEqual(context["services"], [])
        self.assertNotIn("services", dict(context["sections"]))
        self.assertTrue(context["hero_is_niche"])
        self.assertTrue(context["hero"].startswith("data:image/png;base64,"))
        self.assertNotIn("PPF", json.dumps(context["faq"]))
        custom = main.template_context({"business_name": "Example"}, {
            "hero_title": "Custom title", "headline": "Legacy description",
            "services": [{"title": "Explicit service", "description": "Explicit details"}]
        }, niche="detailing_cleaning")
        self.assertEqual(custom["hero_title"], "Custom title")
        self.assertEqual(custom["hero_description"], "Legacy description")
        self.assertIn("services", dict(custom["sections"]))

    def test_customer_hero_precedes_niche_asset_and_remains_validated(self):
        data = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
        (self.base / "hero.png").write_bytes(data)
        context = main.template_context({"business_name": "Example"}, {
            "hero_image_path": "hero.png", "hero_image_position": "top", "image_caption": "Customer caption"
        }, self.base, niche="detailing_cleaning")
        self.assertEqual(context["hero"], "data:image/png;base64," + base64.b64encode(data).decode())
        self.assertFalse(context["hero_is_niche"])
        self.assertEqual(context["image_position"], "top")
        self.assertEqual(context["image_caption"], "Customer caption")
        with self.assertRaisesRegex(ValueError, "Image not found"):
            main.template_context({"business_name": "Example"}, {"hero_image_path": "missing.png"}, self.base, niche="detailing_cleaning")

    def test_missing_niche_image_uses_graphic_fallback(self):
        _, preset = main.resolve_preset("detailing_cleaning")
        output = io.StringIO()
        with patch("main.resolve_preset", return_value=("detailing_cleaning", dict(preset, hero_image_path="missing-niche.png"))):
            with contextlib.redirect_stdout(output):
                context = main.template_context({"business_name": "Example"}, {}, niche="detailing_cleaning")
        self.assertIsNone(context["hero"])
        self.assertFalse(context["hero_is_niche"])
        self.assertEqual(context["visual_motif"], "detailing")
        self.assertIn("Using the graphic fallback", output.getvalue())

    def test_detailing_theme_caption_and_service_cta(self):
        self.csv([{"business_name": "Example", "place_id": "one", "phone_number": "+37400000001"}])
        self.overrides({"one": {"niche": "detailing_cleaning"}})
        html = self.run_generator(["one"])["generated"][0].read_text(encoding="utf-8")
        self.assertIn('<body class="has-phone theme-detailing">', html)
        self.assertIn("ոչ բիզնեսի իրական մեքենայի կամ սրահի լուսանկար", html)
        self.assertIn('class="text-link" href="#contact"', html)
        self.assertNotIn('id="services"', html)
        self.overrides({"one": {"niche": "detailing_cleaning", "services": [{"title": "Explicit", "description": "Only supplied"}]}})
        html = self.run_generator(["one"], overwrite_existing=True)["generated"][0].read_text(encoding="utf-8")
        self.assertIn('class="text-link" href="#services"', html)
        for niche in ("general_service", "passenger_transport"):
            self.overrides({"one": {"niche": niche}})
            html = self.run_generator(["one"], overwrite_existing=True)["generated"][0].read_text(encoding="utf-8")
            self.assertIn('<body class="has-phone">', html)
            self.assertNotIn('<img class="hero-image"', html)

    def test_customer_logo_precedes_detailing_concept_mark(self):
        data = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
        (self.base / "logo.png").write_bytes(data)
        self.csv([{"business_name": "Example", "place_id": "one"}])
        self.overrides({"one": {"niche": "detailing_cleaning", "logo_path": "logo.png"}})
        html = self.run_generator(["one"])["generated"][0].read_text(encoding="utf-8")
        header = html.split('<header class="site-header">')[1].split('</header>')[0]
        self.assertIn('<img class="logo" src="data:image/png;base64,', header)
        self.assertNotIn('<svg class="brand-symbol"', header)


if __name__ == "__main__":
    unittest.main()
