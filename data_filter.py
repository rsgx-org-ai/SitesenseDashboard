"""Cut data.json down to the projects one user is allowed to see.

Hiding other projects in the browser is not enough: the whole payload ships in
the page, so anyone can read it in the page source. This builds a genuinely
smaller payload instead - only the allowed projects' rows, and only the
strings and materials those rows reference - so names, vendors and values
from other projects are never sent at all.

data.json stores text once in a shared `strings` table and rows refer to it by
index, so each row's text columns must be re-pointed into a new, smaller table.
SCHEMA records which columns are text for every row type. It must match the
row layouts written by fetch_data.py; if a row's length ever differs from what
is recorded here, filtering refuses (FilterError) rather than guessing, since a
column wrongly treated as a number would keep pointing into the old table and
could surface another project's text.
"""

# table -> (row length, text columns). Column 0 is always the project index and
# column 1 the material index (re-pointed separately).
SCHEMA = {
    "inventory": (16, [13, 14, 15]),
    "storage":   (5,  [2, 3]),
    "transfers": (9,  [2, 3, 5, 6, 7, 8]),
    "assets":    (11, [2, 3, 9, 10]),
    "receipts":  (18, [2, 3, 4, 5, 6, 7, 8, 9, 10, 13, 14, 15, 16, 17]),
    "fim":       (10, [2, 3, 4, 6]),
    "issuing":   (14, [2, 3, 4, 5, 6, 11, 12, 13]),
}
MATERIAL_LEN = 5            # [name, desc, uom, type, guid] - all text
MATERIAL_GUID_COL = 4
LEDGER_DOC = (5, [1, 2, 3, 4])   # [project, status, month, name, updated]

KNOWN_KEYS = {"generated_at", "strings", "projects", "materials",
              "delivery_dates", "ledger_types", *SCHEMA}


class FilterError(RuntimeError):
    """data.json does not match SCHEMA; serving a filtered copy is unsafe."""


def filter_data(data, allowed_project_ids):
    """Return a copy of `data` holding only the projects in allowed_project_ids."""
    unknown = set(data) - KNOWN_KEYS
    if unknown:
        raise FilterError(f"data.json has fields data_filter.py does not know how to "
                          f"filter: {sorted(unknown)}. Add them to SCHEMA.")

    allowed = {str(p) for p in allowed_project_ids}
    old_strings, old_materials = data["strings"], data["materials"]

    new_strings, string_map = [""], {0: 0}

    def s(i):
        got = string_map.get(i)
        if got is None:
            if not isinstance(i, int) or not 0 <= i < len(old_strings):
                raise FilterError(f"text column held {i!r}, not a valid string index")
            got = len(new_strings)
            string_map[i] = got
            new_strings.append(old_strings[i])
        return got

    new_materials, material_map = [], {}

    def m(i):
        got = material_map.get(i)
        if got is None:
            row = old_materials[i]
            if len(row) != MATERIAL_LEN:
                raise FilterError(f"material row has {len(row)} columns, expected {MATERIAL_LEN}")
            got = len(new_materials)
            material_map[i] = got
            new_materials.append([s(x) for x in row])
        return got

    project_map, new_projects = {}, []
    for i, p in enumerate(data["projects"]):
        if str(p["id"]) in allowed:
            project_map[i] = len(new_projects)
            new_projects.append(dict(p))

    out = {"generated_at": data["generated_at"], "projects": new_projects}

    for table, (length, text_cols) in SCHEMA.items():
        rows = []
        for row in data[table]:
            if len(row) != length:
                raise FilterError(f"{table} row has {len(row)} columns, expected {length}")
            if row[0] not in project_map:
                continue
            new = list(row)
            new[0] = project_map[row[0]]
            new[1] = m(row[1])
            for c in text_cols:
                new[c] = s(row[c])
            rows.append(new)
        out[table] = rows

    ledgers = {}
    for key, entry in data["ledger_types"].items():
        docs = []
        for d in entry["docs"]:
            if len(d) != LEDGER_DOC[0]:
                raise FilterError(f"ledger {key} doc has {len(d)} columns, expected {LEDGER_DOC[0]}")
            if d[0] not in project_map:
                continue
            new = list(d)
            new[0] = project_map[d[0]]
            for c in LEDGER_DOC[1]:
                new[c] = s(d[c])
            docs.append(new)
        ledgers[key] = {**{k: v for k, v in entry.items() if k != "docs"}, "docs": docs}
    out["ledger_types"] = ledgers

    # Delivery dates are keyed by material GUID: keep only materials this user can see.
    visible_guids = {new_strings[row[MATERIAL_GUID_COL]] for row in new_materials}
    out["delivery_dates"] = {g: v for g, v in (data.get("delivery_dates") or {}).items()
                             if g in visible_guids}

    out["materials"] = new_materials
    out["strings"] = new_strings
    return out
