"""Local CSV-to-HTML concepts and offline screenshots, with durable progress."""
from contextlib import contextmanager
import base64
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import tempfile
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape

BASE_DIR = Path(__file__).resolve().parent
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000


def local_path(value, base=BASE_DIR):
    path = Path(value)
    return path if path.is_absolute() else base / path


def safe_url(value):
    if not value or re.search(r"[\s\x00-\x1f\x7f]", value) or "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() in {"http", "https"} and parts.hostname and not parts.username and not parts.password:
            _ = parts.port
            return value
    except ValueError:
        pass
    return None


def phone_values(value):
    # Remove only business_finder's single leading Excel presentation escape.
    display = value[1:] if value.startswith("'") else value
    if not re.fullmatch(r"\+?[0-9 () .-]+", display):
        return display, None
    normalized = re.sub(r"[ () .-]", "", display)
    return display, "tel:" + normalized if 7 <= len(normalized.lstrip("+")) <= 15 else None


def output_name(name, place_id):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60].rstrip("-") or "business"
    return f"{slug}-{hashlib.sha256(place_id.encode('utf-8')).hexdigest()[:12]}"


def read_selected(path, selected):
    rows, errors = {}, {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = {"business_name", "place_id"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError("Missing required CSV columns: " + ", ".join(sorted(missing)))
        for line, raw in enumerate(reader, 2):
            place_id = raw.get("place_id") or ""
            if place_id not in selected:
                continue
            row = {key: (value or "").strip() for key, value in raw.items() if key is not None}
            if None in raw or any(value is None for value in raw.values()):
                errors[place_id] = f"CSV row {line}: wrong number of columns"
            elif not row["business_name"] or not row["place_id"]:
                errors[place_id] = f"CSV row {line}: business_name and place_id must be nonempty"
            elif place_id in rows and rows[place_id] != row:
                errors[place_id] = "Conflicting duplicate rows for selected place_id"
            else:
                rows[place_id] = row
    for place_id in selected:
        if place_id not in rows and place_id not in errors:
            errors[place_id] = "Selected place_id not found (IDs must match exactly)"
    return rows, errors


def load_personalization(path):
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError("personalization.json must be an object keyed by place_id")
    return data


def embedded_image(value, base=BASE_DIR):
    """Read bounded local raster files, recognizing type and dimensions by header."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Image path must be a nonempty local filename")
    path = local_path(value, base)
    if not path.is_file():
        raise ValueError(f"Image not found: {value}. Supply an existing local raster image.")
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError(f"Image exceeds 4 MiB: {value}. Resize or compress it.")
    data = path.read_bytes()
    mime, dimensions = None, None
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        mime, dimensions = "image/png", struct.unpack(">II", data[16:24])
    elif data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        mime, dimensions = "image/gif", struct.unpack("<HH", data[6:10])
    elif data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        mime = "image/jpeg"
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 255:
                break
            marker = data[offset + 1]
            if marker == 255:
                offset += 1
                continue
            if marker in (0xD9, 0xDA):
                break
            size = int.from_bytes(data[offset + 2:offset + 4], "big")
            if size < 2:
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and offset + 9 <= len(data):
                height, width = struct.unpack(">HH", data[offset + 5:offset + 9])
                dimensions = width, height
                break
            offset += 2 + size
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
        if data[12:16] == b"VP8X" and len(data) >= 30:
            dimensions = (1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little"))
        elif data[12:16] == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            dimensions = tuple(n & 0x3FFF for n in struct.unpack("<HH", data[26:30]))
        elif data[12:16] == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            dimensions = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if not mime or not dimensions or min(dimensions) < 1:
        raise ValueError(f"Unsupported or invalid image: {value}. Use PNG, JPEG, GIF or WebP.")
    if dimensions[0] * dimensions[1] > MAX_IMAGE_PIXELS:
        raise ValueError(f"Image exceeds 24 megapixels: {value}. Resize it.")
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def resolve_preset(niche):
    """Explicit niches; never classify a business by its listing name."""
    with (BASE_DIR / "presets.json").open(encoding="utf-8") as stream:
        presets = json.load(stream)
    if not isinstance(niche, str) or niche not in presets:
        print(f"Unsupported niche {niche!r}; falling back to general_service.")
        niche = "general_service"
    return niche, presets[niche]


def text_pairs(value, keys, field):
    if not isinstance(value, list) or any(
        not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key].strip()
                                         for key in keys) for item in value):
        raise ValueError(f"{field} must be a list of objects with {' and '.join(keys)} text")
    return value


def template_context(row, overrides, base=BASE_DIR, niche="general_service"):
    if not isinstance(overrides, dict):
        raise ValueError("Personalization for this ID must be an object")
    for key in ("display_name", "brand_name", "hero_title", "hero_description", "headline",
                "introduction", "business_type_label", "image_caption"):
        if key in overrides and (not isinstance(overrides[key], str) or not overrides[key].strip()):
            raise ValueError(f"{key} must be nonempty text")
    niche, preset = resolve_preset(overrides.get("niche", niche))
    accent = overrides.get("accent_color", preset.get("accent_color", "#9b4935"))
    if not isinstance(accent, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3}", accent):
        raise ValueError("accent_color must be a hex color such as #9b4935")
    services = text_pairs(overrides.get("services", []), ("title", "description"), "services")
    faq = text_pairs(overrides.get("faq", preset["faq"]), ("question", "answer"), "faq")
    name = overrides.get("brand_name", overrides.get("display_name", row["business_name"]))
    route = overrides.get("route")
    if route is not None and (not isinstance(route, dict) or any(
        not isinstance(route.get(key), str) or not route[key].strip() for key in ("from", "to"))):
        raise ValueError('route must contain nonempty "from" and "to" text')
    # Route presentation is exclusive to an explicitly selected transport niche.
    route = route if niche == "passenger_transport" else None
    image_position = overrides.get("hero_image_position", "center")
    if image_position not in ("center", "top", "bottom"):
        raise ValueError("hero_image_position must be center, top or bottom")
    hero, hero_is_niche = None, False
    if "hero_image_path" in overrides:
        # An invalid customer override is an error, never silently substituted.
        hero = embedded_image(overrides["hero_image_path"], base)
    elif preset.get("hero_image_path"):
        niche_image = local_path(preset["hero_image_path"], BASE_DIR)
        if niche_image.is_file():
            hero = embedded_image(str(niche_image))
            hero_is_niche = True
        else:
            print(f"Niche image not found: {niche_image}. Using the graphic fallback.")
    phone, telephone = phone_values(row.get("phone_number", ""))
    links = [(label, safe_url(row.get(key, ""))) for key, label in (
        ("instagram_url", "Instagram"), ("facebook_url", "Facebook"),
        ("google_maps_url", "Քարտեզ"), ("website", "Կայք"))]
    links = [(label, url) for label, url in links if url]
    social = next(((label, url) for label, url in links if label in ("Instagram", "Facebook")), None)
    process = []
    if telephone or social:
        first = {"title": "Զանգահարեք" if telephone else f"Գրեք մեզ՝ {social[0]}",
                 "description": "Կապ հաստատեք ստորև նշված տվյալներով և սկսեք զրույցը։"}
        process = [first, *preset["process"]]
    labels = preset["labels"]
    sections = [("route", labels["route"])] if route else [("about", labels["about"])]
    if services:
        sections.append(("services", labels["services"]))
    if process:
        sections.append(("process", labels["process"]))
    if faq:
        sections.append(("faq", labels["faq"]))
    sections.append(("contact", labels["contact"]))
    # Accent remains decorative; text/buttons use the high-contrast ink palette.
    return dict(name=name, brand_name=name, business_name=row["business_name"], niche=niche,
                hero_title=overrides.get("hero_title", preset["hero_title"]),
                hero_description=overrides.get("hero_description", overrides.get("headline", preset["hero_description"])),
                route=route, faq=faq, process=process, sections=sections, labels=labels,
                visual_motif=preset["visual_motif"],
                business_type_label=overrides.get("business_type_label", preset["business_type_label"]),
                hero_is_niche=hero_is_niche,
                image_position=image_position, image_caption=overrides.get("image_caption", ""),
                introduction=overrides.get("introduction", preset["introduction"]),
                services=services, accent=accent, phone=phone, telephone=telephone,
                address=row.get("address", ""), links=links,
                logo=embedded_image(overrides["logo_path"], base) if "logo_path" in overrides else None,
                hero=hero)


def write_atomic(path, content, overwrite=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # Atomic create-if-absent: also protects against concurrent runs.
            os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def generate(input_csv, output_directory, selected_place_ids, max_previews=5,
             overwrite_existing=False, base=BASE_DIR, generate_screenshots=False,
             niche="general_service", personalization_file="personalization.json"):
    result = {"generated": [], "skipped": [], "failed": []}
    selected = list(dict.fromkeys(selected_place_ids))
    if not selected:
        print('No businesses selected. Set selected_place_ids in main(), e.g. ["demo-business-001"].')
        print_summary(result)
        return result
    if not isinstance(max_previews, int) or isinstance(max_previews, bool) or max_previews < 1:
        raise ValueError("max_previews must be a positive integer")
    if len(selected) > max_previews:
        print(f"Selection limited to the first {max_previews} IDs; {len(selected) - max_previews} deferred.")
    selected = selected[:max_previews]
    try:
        rows, errors = read_selected(local_path(input_csv, base), selected)
        personalization = load_personalization(local_path(personalization_file, base))
        environment = Environment(loader=FileSystemLoader(BASE_DIR / "templates"),
                                  autoescape=select_autoescape(["html"]))
        template = environment.get_template("service_business.html")
    except (OSError, ValueError, csv.Error) as error:
        result["failed"] = [(place_id, str(error)) for place_id in selected]
        print_summary(result)
        return result
    for place_id in selected:
        try:
            if place_id in errors:
                raise ValueError(errors[place_id])
            row = rows[place_id]
            destination = local_path(output_directory, base) / output_name(row["business_name"], place_id) / "index.html"
            if destination.exists() and not overwrite_existing:
                result["skipped"].append(destination)
                continue
            context = template_context(row, personalization.get(place_id, {}), base, niche)
            try:
                write_atomic(destination, template.render(**context), overwrite_existing)
            except FileExistsError:
                result["skipped"].append(destination)
                continue
            result["generated"].append(destination)
        except (OSError, ValueError) as error:
            result["failed"].append((place_id, str(error)))
    print_summary(result)
    if generate_screenshots:
        # Lazy import keeps normal generation independent of Playwright.
        from screenshot import capture_batch, print_capture_summary
        result["screenshots"] = capture_batch(result["generated"])
        print_capture_summary(result["screenshots"])
    return result


def print_summary(result):
    print(f"Generated: {len(result['generated'])} | Skipped: {len(result['skipped'])} | Failed: {len(result['failed'])}")
    for status in ("generated", "skipped"):
        for path in result[status]:
            print(f"{status}: {path}")
    for place_id, reason in result["failed"]:
        print(f"failed [{place_id!r}]: {reason}")


class CSVSaveError(OSError):
    """Tracking could not be committed; stop before attempting another business."""


def save_csv_atomic(path, fields, rows):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise CSVSaveError(f"Could not save CSV {path}: {error}. Close Excel or any program locking this file, check write permissions, and rerun. The previous valid CSV was preserved.") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def csv_lock(path):
    """A persistent sidecar with an OS lock automatically released on exit/crash."""
    with path.with_name(path.name + ".lock").open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError(f"Another instance is processing {path}. Wait for it to finish and rerun.") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def is_processed(row):
    return row.get("processed", "").strip().upper() == "TRUE"


def read_batch(path):
    """Preserve every cell exactly; validate the whole CSV before any processing."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        missing = {"business_name", "place_id"} - set(fields)
        if missing:
            raise ValueError("Missing required CSV columns: " + ", ".join(sorted(missing)))
        if len(fields) != len(set(fields)) or any(not field for field in fields):
            raise ValueError("CSV column headers must be nonempty and unique; CSV was not changed")
        rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError("CSV has rows with the wrong number of cells. Fix the CSV before rerunning; CSV was not changed")
    groups, failures = {}, []
    for index, row in enumerate(rows):
        if not row["place_id"].strip() or not row["business_name"].strip():
            failures.append((f"CSV row {index + 2}", "business_name and place_id must be nonempty"))
            # Keep invalid rows in a group too so they cannot hide an ID conflict.
        groups.setdefault(row["place_id"], []).append(index)
    candidates = []
    for place_id, indices in groups.items():
        first = rows[indices[0]]
        comparable = lambda row: {key: value for key, value in row.items() if key != "processed"}
        if any(comparable(rows[index]) != comparable(first) for index in indices[1:]):
            failures.append((place_id, "Conflicting duplicate place_id at CSV rows " + ", ".join(str(index + 2) for index in indices)))
            continue
        if not first["place_id"].strip() or not first["business_name"].strip():
            continue
        if any(not is_processed(rows[index]) for index in indices):
            candidates.append((place_id, indices))
    candidates.sort(key=lambda item: next(index for index in item[1] if not is_processed(rows[index])))
    return fields, rows, candidates, failures


def print_batch_summary(result):
    print(f"Rows already processed: {result['already_processed']}")
    print(f"Businesses attempted: {result['attempted']}")
    print(f"Businesses completed: {len(result['completed'])}")
    print(f"Failures: {len(result['failed'])}")
    for label, reason in result["failed"]:
        print(f"failed [{label!r}]: {reason}")
    remaining = result["remaining"] if result["remaining"] is not None else "unknown (CSV could not be read)"
    print(f"Remaining unprocessed rows: {remaining}")
    print(f"Output location: {result['output_location']}")
    for path in result["completed"]:
        print(f"completed: {path.parent}")
    if result["fatal"]:
        print(f"Stopped: {result['fatal']}")


def process_csv(input_csv, output_directory="output", max_previews=10, base=BASE_DIR,
                niche="general_service", personalization_file="personalization.json"):
    """Generate and track automatic batches; never infer completion from files."""
    path = local_path(input_csv, base).resolve()
    output = local_path(output_directory, base).resolve()
    result = dict(already_processed=0, attempted=0, completed=[], failed=[],
                  remaining=0, output_location=output, fatal=None)
    rows = None
    try:
        if not isinstance(max_previews, int) or isinstance(max_previews, bool) or max_previews < 1:
            raise ValueError("max_previews must be a positive integer")
        if not path.is_file():
            raise ValueError(f"Input CSV not found: {path}. Put your CSV here or edit input_csv in main().")
        with csv_lock(path):
            fields, rows, candidates, result["failed"] = read_batch(path)
            result["already_processed"] = sum(is_processed(row) for row in rows)
            final_fields = [field for field in fields if field != "processed"] + ["processed"]
            for row in rows:
                row.setdefault("processed", "")
            if fields != final_fields:
                save_csv_atomic(path, final_fields, rows)
            if candidates:
                personalization = load_personalization(local_path(personalization_file, base))
                environment = Environment(loader=FileSystemLoader(BASE_DIR / "templates"),
                                          autoescape=select_autoescape(["html"]))
                template = environment.get_template("service_business.html")
                from screenshot import ScreenshotSession, validate_png
                with ScreenshotSession() as session:
                    for place_id, indices in candidates[:max_previews]:
                        result["attempted"] += 1
                        try:
                            # Rendering can trim presentation whitespace; stored cells stay untouched.
                            row = {key: value.strip() for key, value in rows[indices[0]].items()}
                            row["place_id"] = place_id
                            destination = output / output_name(row["business_name"], place_id) / "index.html"
                            context = template_context(row, personalization.get(place_id, {}), base, niche)
                            write_atomic(destination, template.render(**context), overwrite=True)
                            session.capture(destination)
                            if not destination.is_file() or not destination.stat().st_size:
                                raise ValueError("Generated HTML is missing or empty")
                            validate_png(destination.parent / "desktop.png", (1440, 1000))
                            validate_png(destination.parent / "mobile.png", (390, 844))
                        except Exception as error:
                            result["failed"].append((place_id, str(error)))
                            continue
                        previous_flags = [rows[index]["processed"] for index in indices]
                        for index in indices:
                            rows[index]["processed"] = "TRUE"
                        try:
                            save_csv_atomic(path, final_fields, rows)
                        except CSVSaveError:
                            for index, flag in zip(indices, previous_flags):
                                rows[index]["processed"] = flag
                            raise
                        result["completed"].append(destination)
    except (OSError, ValueError, csv.Error) as error:
        result["fatal"] = str(error)
    finally:
        result["remaining"] = sum(not is_processed(row) for row in rows) if rows is not None else None
    print_batch_summary(result)
    return result


def main():
    input_csv = "input/businesses.csv"
    output_directory = "output"
    max_previews = 10
    niche = "detailing_cleaning"  # Also passenger_transport or detailing_cleaning; per-business overrides win.
    personalization_file = "personalization.json"
    result = process_csv(input_csv, output_directory, max_previews,
                         niche=niche, personalization_file=personalization_file)
    return 1 if result["failed"] or result["fatal"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
