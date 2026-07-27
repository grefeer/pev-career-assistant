import sqlite3

db = sqlite3.connect("output/personalized_discovery/discovery.db")
db.row_factory = sqlite3.Row
for slug_frag, host in (("pddglobalhr", "pdd"), ("iflytek%zhiye", "iflytek")):
    rows = db.execute(
        "select c.apply_url, c.title from discovered_job_candidates c "
        "join job_discovery_tasks t on t.id=c.task_id "
        "where t.source_url like ? and c.apply_url is not null "
        "limit 3",
        (f"%{slug_frag}%",),
    ).fetchall()
    print(f"=== {host} ===")
    for r in rows:
        print("  ", r["apply_url"])
