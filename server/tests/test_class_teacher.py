# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Class teachers can issue new one-time passwords to the students of their groups."""

from tests.conftest import login_as, make_client


async def setup_school(admin):
    """10.B taught by the teacher who'll log in; 9.A taught by nobody."""
    b10 = (await admin.post("/api/v1/groups", json={"name": "10.B", "kind": "class"})).json()
    a9 = (await admin.post("/api/v1/groups", json={"name": "9.A", "kind": "class"})).json()

    async def user(username, role, groups):
        body = {"username": username, "roles": [{"role": role}] if role else [], "group_ids": [g["id"] for g in groups]}
        r = await admin.post("/api/v1/users", json=body)
        assert r.status_code == 201, r.text
        return r.json()

    mine = await user("diak.mine", "student", [b10])
    other = await user("diak.other", "student", [a9])
    colleague = await user("tanar.colleague", "teacher", [b10])  # a teacher who is *in* 10.B
    return b10, a9, mine, other, colleague


async def test_group_teachers_assignment(app, admin):
    b10, _a9, mine, _other, colleague = await setup_school(admin)
    # only teachers qualify
    r = await admin.put(f"/api/v1/groups/{b10['id']}/teachers", json=[mine["id"]])
    assert r.status_code == 422
    r = await admin.put(f"/api/v1/groups/{b10['id']}/teachers", json=[colleague["id"]])
    assert r.status_code == 200 and [t["username"] for t in r.json()] == ["tanar.colleague"]
    groups = {g["name"]: g for g in (await admin.get("/api/v1/groups")).json()}
    assert [t["username"] for t in groups["10.B"]["teachers"]] == ["tanar.colleague"]
    assert groups["10.B"]["member_count"] == 2  # teachers are not members
    async with login_as(app, admin, "tanar_x", [{"role": "teacher"}]) as teacher:
        assert (await teacher.get(f"/api/v1/groups/{b10['id']}/teachers")).status_code == 200
        assert (await teacher.put(f"/api/v1/groups/{b10['id']}/teachers", json=[])).status_code == 403


async def test_class_teacher_resets_only_own_students(app, admin):
    b10, _a9, mine, other, colleague = await setup_school(admin)
    async with login_as(app, admin, "osztalyfonok", [{"role": "teacher"}]) as teacher:
        me = (await teacher.get("/api/v1/auth/me")).json()
        await admin.put(f"/api/v1/groups/{b10['id']}/teachers", json=[me["id"]])
        admin_id = next(u["id"] for u in (await admin.get("/api/v1/users", params={"q": "admin"})).json()["items"])

        # the list tells the UI exactly whom this teacher may reset
        listed = {u["username"]: u for u in (await teacher.get("/api/v1/users")).json()["items"]}
        assert listed["diak.mine"]["can_reset_password"] is True
        assert listed["diak.mine"]["otp_status"] == "active"  # visible for their own students
        for name in ("diak.other", "tanar.colleague", "admin", "osztalyfonok"):
            assert listed[name]["can_reset_password"] is False, name
            assert listed[name]["otp_status"] is None, name

        r = await teacher.post(f"/api/v1/users/{mine['id']}/password", json={})
        assert r.status_code == 200
        otp = r.json()["one_time_password"]
        assert otp and otp != mine["one_time_password"]

        for target, why in [
            (other["id"], "student of a class they don't teach"),
            (colleague["id"], "a teacher, even though in their class"),
            (admin_id, "an admin"),
            (me["id"], "themselves"),
        ]:
            r = await teacher.post(f"/api/v1/users/{target}/password", json={})
            assert r.status_code == 403, why
        # no typed passwords for teachers, not even for their own students
        r = await teacher.post(f"/api/v1/users/{mine['id']}/password", json={"password": "Tavaszi-Szel-42"})
        assert r.status_code == 403

    async with make_client(app) as c:
        assert (await c.post("/api/v1/auth/login", json={"username": "diak.mine", "password": mine["one_time_password"]})).status_code == 401
        r = await c.post("/api/v1/auth/login", json={"username": "diak.mine", "password": otp})
        assert r.status_code == 200 and r.json()["must_change_password"] is True

    entry = (await admin.get("/api/v1/audit", params={"action": "core.user.password_reset"})).json()[0]
    assert entry["actor_name"] == "osztalyfonok" and entry["details"]["as"] == "class_teacher"
    assert otp not in str(entry)


async def test_teacher_without_classes_and_students_cannot_reset(app, admin):
    _b10, _a9, mine, _other, _colleague = await setup_school(admin)
    async with login_as(app, admin, "tanar_free", [{"role": "teacher"}]) as teacher:
        assert (await teacher.post(f"/api/v1/users/{mine['id']}/password", json={})).status_code == 403
    async with login_as(app, admin, "diak_s", [{"role": "student"}]) as student:
        r = await student.post(f"/api/v1/users/{mine['id']}/password", json={})
        assert r.status_code == 403
        # no permission -> refused before the user id is even looked up (no existence probe)
        r = await student.post("/api/v1/users/00000000-0000-0000-0000-000000000000/password", json={})
        assert r.status_code == 403


async def test_removing_class_teacher_revokes_access(app, admin):
    b10, _a9, mine, _other, _colleague = await setup_school(admin)
    async with login_as(app, admin, "tanar_rm", [{"role": "teacher"}]) as teacher:
        me = (await teacher.get("/api/v1/auth/me")).json()
        await admin.put(f"/api/v1/groups/{b10['id']}/teachers", json=[me["id"]])
        assert (await teacher.post(f"/api/v1/users/{mine['id']}/password", json={})).status_code == 200
        await admin.put(f"/api/v1/groups/{b10['id']}/teachers", json=[])
        assert (await teacher.post(f"/api/v1/users/{mine['id']}/password", json={})).status_code == 403


async def test_class_teacher_downloads_slips_for_own_students_only(app, admin):
    import io

    from openpyxl import load_workbook

    b10, a9, mine, other, colleague = await setup_school(admin)

    def usernames(content: bytes) -> set[str]:
        rows = list(load_workbook(io.BytesIO(content)).active.iter_rows(values_only=True))
        assert rows[0][3] == "One-time password"
        return {r[1] for r in rows[1:]}

    async with login_as(app, admin, "osztalyfonok2", [{"role": "teacher"}]) as teacher:
        # not a class teacher yet: allowed, but nothing to export
        assert usernames((await teacher.get("/api/v1/users/credentials.xlsx")).content) == set()
        me = (await teacher.get("/api/v1/auth/me")).json()
        await admin.put(f"/api/v1/groups/{b10['id']}/teachers", json=[me["id"]])
        r = await teacher.get("/api/v1/users/credentials.xlsx")
        assert r.status_code == 200
        # only their student; never the colleague in the same group or other classes
        assert usernames(r.content) == {"diak.mine"}
        assert usernames((await teacher.get("/api/v1/users/credentials.xlsx", params={"group_id": a9["id"]})).content) == set()
    entries = (await admin.get("/api/v1/audit", params={"action": "core.user.credentials_export"})).json()
    assert all(e["actor_name"] == "osztalyfonok2" and e["details"]["as"] == "class_teacher" for e in entries)
    assert [e["details"]["usernames"] for e in reversed(entries)] == [[], ["diak.mine"], []]
    # admins still get everyone with an active one-time password
    assert {"diak.mine", "diak.other", "tanar.colleague"} <= usernames((await admin.get("/api/v1/users/credentials.xlsx")).content)
    async with login_as(app, admin, "diak_slip", [{"role": "student"}]) as student:
        assert (await student.get("/api/v1/users/credentials.xlsx")).status_code == 403
