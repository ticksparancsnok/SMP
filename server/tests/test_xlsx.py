"""User export / import (.xlsx)."""

import io
from pathlib import Path

from openpyxl import Workbook, load_workbook

from smp.core.xlsx_users import HEADER, SAMPLE_ROWS, XLSX_MIME, read_users
from tests.conftest import login_as, make_client

SAMPLE_FILE = Path(__file__).resolve().parents[2] / "docs" / "samples" / "felhasznalok-minta.xlsx"


def xlsx(rows, header=HEADER) -> bytes:
    wb = Workbook()
    ws = wb.active
    if header:
        ws.append(list(header))
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def read(data: bytes) -> list[tuple]:
    ws = load_workbook(io.BytesIO(data)).active
    return [tuple(c.value for c in row) for row in ws.iter_rows()]


async def post_import(client, data: bytes, dry_run=False):
    return await client.post(
        "/api/v1/users/import",
        params={"dry_run": str(dry_run).lower()},
        content=data,
        headers={"content-type": XLSX_MIME},
    )


# --------------------------------------------------------------------------- export


async def test_export_uses_current_filters(admin):
    a9 = (await admin.post("/api/v1/groups", json={"name": "9.A", "kind": "class"})).json()
    b10 = (await admin.post("/api/v1/groups", json={"name": "10.B", "kind": "class"})).json()
    club = (await admin.post("/api/v1/groups", json={"name": "Kosárlabda", "kind": "custom"})).json()
    await admin.post("/api/v1/users", json={"username": "kiss.p", "display_name": "Kiss Péter", "group_ids": [a9["id"], club["id"]], "roles": [{"role": "student"}]})
    await admin.post("/api/v1/users", json={"username": "nagy.a", "display_name": "Nagy Anna", "group_ids": [a9["id"], b10["id"]], "roles": [{"role": "student"}]})
    await admin.post("/api/v1/users", json={"username": "tanar.t", "display_name": "Tóth Tanár", "roles": [{"role": "teacher"}]})

    r = await admin.get("/api/v1/users/export.xlsx")
    assert r.status_code == 200 and r.headers["content-type"] == XLSX_MIME
    assert "attachment" in r.headers["content-disposition"]
    rows = read(r.content)
    assert rows[0] == HEADER
    by_user = {row[1]: row for row in rows[1:]}
    assert set(by_user) == {"admin", "kiss.p", "nagy.a", "tanar.t"}
    # only class-type groups, comma separated
    assert by_user["kiss.p"] == ("Kiss Péter", "kiss.p", "9.A")
    assert by_user["nagy.a"] == ("Nagy Anna", "nagy.a", "9.A, 10.B")  # natural order
    assert by_user["tanar.t"][2] in (None, "")

    # each filter narrows the export exactly like the list
    for params, expected in [
        ({"role": "student"}, {"kiss.p", "nagy.a"}),
        ({"group_id": b10["id"]}, {"nagy.a"}),
        ({"q": "anna"}, {"nagy.a"}),
        ({"role": "student", "group_id": club["id"]}, {"kiss.p"}),
    ]:
        listed = {u["username"] for u in (await admin.get("/api/v1/users", params=params)).json()["items"]}
        exported = {row[1] for row in read((await admin.get("/api/v1/users/export.xlsx", params=params)).content)[1:]}
        assert exported == listed == expected, params

    entry = (await admin.get("/api/v1/audit", params={"action": "core.user.export"})).json()[0]
    assert entry["details"]["count"] == 1  # the last export


async def test_export_neutralises_formulas(admin):
    await admin.post("/api/v1/users", json={"username": "evil", "display_name": '=HYPERLINK("http://x","y")'})
    ws = load_workbook(io.BytesIO((await admin.get("/api/v1/users/export.xlsx", params={"q": "evil"})).content)).active
    cell = ws["A2"]
    assert cell.value == '=HYPERLINK("http://x","y")' and cell.data_type == "s"


async def test_export_and_sample_permissions(app, admin):
    async with login_as(app, admin, "tanar_x", [{"role": "teacher"}]) as teacher:
        assert (await teacher.get("/api/v1/users/export.xlsx")).status_code == 200
        assert (await teacher.get("/api/v1/users/import-sample.xlsx")).status_code == 200
    async with login_as(app, admin, "diak_x", [{"role": "student"}]) as student:
        assert (await student.get("/api/v1/users/export.xlsx")).status_code == 403
        assert (await student.get("/api/v1/users/import-sample.xlsx")).status_code == 403


async def test_sample_file_matches_format(admin):
    served = read((await admin.get("/api/v1/users/import-sample.xlsx")).content)
    committed = read(SAMPLE_FILE.read_bytes())
    assert served == committed == [HEADER, *SAMPLE_ROWS]
    parsed = read_users(SAMPLE_FILE.read_bytes())
    assert not parsed.errors and len(parsed.rows) == len(SAMPLE_ROWS)


# --------------------------------------------------------------------------- import


async def test_import_dry_run_then_import(app, admin):
    await admin.post("/api/v1/groups", json={"name": "9.A", "kind": "class"})
    await admin.post("/api/v1/users", json={"username": "mar.van", "display_name": "Már Van"})
    data = xlsx([
        ("Kiss Péter", "kiss.peter", "9.A"),
        ("Nagy Anna", "nagy.anna", " 9.A , 9.C "),
        (None, None, None),  # blank line is ignored
        ("Már Van", "mar.van", "9.C"),  # exists -> skipped, untouched
        ("", "nev.nelkul", ""),  # no name -> username used
    ])

    r = await post_import(admin, data, dry_run=True)
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview == {
        "dry_run": True,
        "created": ["kiss.peter", "nagy.anna", "nev.nelkul"],
        "skipped": ["mar.van"],
        "groups_created": ["9.C"],
        "errors": [],
    }
    assert (await admin.get("/api/v1/users", params={"q": "kiss.peter"})).json()["total"] == 0  # nothing written

    r = await post_import(admin, data)
    assert r.status_code == 200 and r.json()["dry_run"] is False
    users = {u["username"]: u for u in (await admin.get("/api/v1/users", params={"role": "student"})).json()["items"]}
    assert set(users) == {"kiss.peter", "nagy.anna", "nev.nelkul"}
    anna = users["nagy.anna"]
    assert anna["display_name"] == "Nagy Anna" and anna["must_change_password"] is True
    assert sorted(g["name"] for g in anna["groups"]) == ["9.A", "9.C"]
    assert users["nev.nelkul"]["display_name"] == "nev.nelkul"
    groups = {g["name"]: g for g in (await admin.get("/api/v1/groups")).json()}
    assert groups["9.C"]["kind"] == "class" and groups["9.A"]["member_count"] == 2
    existing = (await admin.get("/api/v1/users", params={"q": "mar.van"})).json()["items"][0]
    assert existing["groups"] == [] and existing["roles"] == []  # skipped means untouched

    # each imported student has their own one-time password, exported as login slips
    slips = read((await admin.get("/api/v1/users/credentials.xlsx")).content)
    otps = {row[1]: row[3] for row in slips[1:]}
    # (mar.van was created earlier without a password, so it has an unused OTP too)
    assert set(otps) == {"kiss.peter", "nagy.anna", "nev.nelkul", "mar.van"}
    assert len(set(otps.values())) == 4  # all different
    assert anna["otp_status"] == "active"
    async with make_client(app) as c:
        me = (await c.post("/api/v1/auth/login", json={"username": "kiss.peter", "password": otps["kiss.peter"]})).json()
        assert me["must_change_password"] is True and me["roles"] == [{"role": "student", "room_id": None}]
        bad = await c.post("/api/v1/auth/login", json={"username": "nagy.anna", "password": otps["kiss.peter"]})
        assert bad.status_code == 401

    entry = (await admin.get("/api/v1/audit", params={"action": "core.user.import"})).json()[0]
    assert entry["details"]["created"] == ["kiss.peter", "nagy.anna", "nev.nelkul"]

    # importing the same file again creates nothing
    again = (await post_import(admin, data)).json()
    assert again["created"] == [] and len(again["skipped"]) == 4


async def test_import_rejects_whole_file_on_errors(admin):
    data = xlsx([
        ("Jó Sor", "jo.sor", "9.A"),
        ("Ékezetes", "kiss.péter", ""),
        ("Nincs Név", "", "9.A"),
        ("Duplikált", "jo.sor", ""),
    ])
    r = await post_import(admin, data)
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert [e["row"] for e in errors] == [3, 4, 5]
    assert "érvénytelen" in errors[0]["error"] and "hiányzik" in errors[1]["error"] and "2. sorban" in errors[2]["error"]
    assert (await admin.get("/api/v1/users", params={"q": "jo.sor"})).json()["total"] == 0  # atomic

    r = await post_import(admin, xlsx([("a", "b", "c")], header=("Name", "Username", "Class")))
    assert r.status_code == 422 and r.json()["detail"]["errors"][0]["row"] == 1
    r = await post_import(admin, b"this is not a spreadsheet")
    assert r.status_code == 422 and r.json()["detail"]["errors"][0]["row"] == 0
    # header matching ignores case/whitespace
    r = await post_import(admin, xlsx([("X Y", "x.y", "")], header=(" név", "FELHASZNÁLÓNÉV", "osztály ")), dry_run=True)
    assert r.status_code == 200 and r.json()["created"] == ["x.y"]


async def test_import_permissions(app, admin):
    async with login_as(app, admin, "tanar_i", [{"role": "teacher"}]) as teacher:
        assert (await post_import(teacher, xlsx([("A", "a.a", "")]))).status_code == 403
