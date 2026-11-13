"""Offline unit tests: discovery rules, parsing, analysis, number guard, cover size."""
import json, sys, tempfile
from pathlib import Path
ROOT = Path(sys.argv[1]); DATA = Path(sys.argv[2])
sys.path.insert(0, str(ROOT))
from dv_discovery import detect_licence, data_links, data_format, search_result_links, unwrap_url, followup_links, licence_links
from dv_acquire import parse, profile
from dv_analysis import analyse, heuristic_roles, highlights, template_text, allowed_numbers, unsupported_numbers, cover, candidate_roles
fails = 0
def t(name, cond, info=""):
    global fails
    print(("ok  " if cond else "FAIL") + f" {name} {info if not cond else ''}")
    fails += 0 if cond else 1

# licences
t("cc-by", detect_licence("Data released under CC BY 4.0")["licence"] == "CC-BY-4.0")
t("cc-by-sa", detect_licence("licensed CC BY-SA 4.0")["licence"] == "CC-BY-SA-4.0")
t("nc rejected", detect_licence("Creative Commons CC BY-NC 4.0")["accepted"] is False)
t("cc0 link", detect_licence("", ["https://creativecommons.org/publicdomain/zero/1.0/"])["licence"] == "CC0")
t("odbl", detect_licence("Open Database License (ODbL)")["licence"] == "ODbL")
t("OGL custom", detect_licence("Open Government Licence v3.0")["licence"] == "Custom")
t("us gov", detect_licence("These data are a U.S. Government work and not subject to copyright")["licence"] == "Custom")
t("weak rejected", detect_licence("data are freely available")["accepted"] is False)
t("none", detect_licence("hello world")["reason"] == "no reuse terms found")
# Real evidence from run 20261113T031922Z
nps = "ownership copyright law does not protect \u201cany work of the u.s. government\u201d where \u201ca work prepared by an officer"
t("NPS US-gov work accepted", detect_licence(nps)["accepted"] and detect_licence(nps)["licence"] == "Custom")
cs = "every dataset is compiled from publicly available sources, and we aim to publish only data that is in the public domain or offered under terms that permit redistribution"
t("aggregator aspiration rejected", detect_licence(cs)["accepted"] is False, detect_licence(cs))
t("these data are public domain", detect_licence("These data are in the public domain.")["accepted"])
t("HDX CC BY International", detect_licence("License: Creative Commons Attribution International")["licence"] == "CC-BY-4.0")
t("HDX CC BY-SA International", detect_licence("Creative Commons Attribution-ShareAlike International")["licence"] == "CC-BY-SA-4.0")
t("Zenodo", detect_licence("Creative Commons Attribution 4.0 International")["licence"] == "CC-BY-4.0")
t("CC BY no version", detect_licence("Licence: Creative Commons Attribution")["label"].startswith("Creative Commons Attribution (version"))
t("NC International rejected", detect_licence("Creative Commons Attribution-NonCommercial 4.0 International")["accepted"] is False)
# formats
t("csv ext", data_format("https://x.org/a/b.csv") == "csv")
t("csv query", data_format("https://x.org/export?format=csv") == "csv")
t("socrata", data_format("https://data.x.gov/api/views/abcd/rows.csv?accessType=DOWNLOAD") == "csv")
t("netcdf unsupported", data_format("https://x.org/sst.nc") == "nc")
t("html none", data_format("https://x.org/page.html") is None)
# DDG unwrap + search filter
ddg = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.psl.noaa.gov%2Fdata%2Fgridded%2Fdata.noaa.oisst.v2.highres.html&rut=abc"
t("ddg unwrap", unwrap_url(ddg, "https://html.duckduckgo.com/html/?q=x") == "https://www.psl.noaa.gov/data/gridded/data.noaa.oisst.v2.highres.html")
links = [{"href": ddg, "text": "NOAA OI SST V2 High Resolution Dataset"}, {"href": "https://duckduckgo.com/settings", "text": "Settings"},
         {"href": "https://www.datavera.io/?dataset=1", "text": "sst on datavera"}]
res = search_result_links(links, "https://html.duckduckgo.com/html/?q=x", "NOAA sea surface temperature sst")
t("search results filtered", len(res) == 1 and "noaa" in res[0]["url"], res)
# hidden data panel from the REAL failed-run fixture semantics
page_links = [{"href": "https://psl.noaa.gov/marine-heatwaves/#data", "text": "Data", "visible": True},
              {"href": "https://psl.noaa.gov/marine-heatwaves/data/mhw_counts.csv", "text": "Monthly counts (CSV)", "visible": False},
              {"href": "https://psl.noaa.gov/marine-heatwaves/data/mhw.nc", "text": "NetCDF", "visible": False},
              {"href": "https://psl.noaa.gov/about/license.html", "text": "Data license", "visible": True},
              {"href": "https://psl.noaa.gov/data/access.html", "text": "Download data", "visible": True}]
dl = data_links(page_links, "https://psl.noaa.gov/marine-heatwaves/", "marine heatwave")
t("hidden csv found first", dl[0]["format"] == "csv" and dl[0]["hidden"], dl)
t("licence link", licence_links(page_links, "https://psl.noaa.gov/marine-heatwaves/") == ["https://psl.noaa.gov/about/license.html"])
t("followup link", [f["url"] for f in followup_links(page_links, "https://psl.noaa.gov/marine-heatwaves/")] == ["https://psl.noaa.gov/data/access.html"])

# parse + analyse three real shapes
for name, expect_cat in (("glaciers.csv", False), ("co2-annmean-mlo.csv", False), ("population.csv", True)):
    df, notes = parse(DATA / name, "csv")
    prof = profile(df)
    roles = heuristic_roles(prof)
    if name == "population.csv":
        roles = {"time": "Year", "category": "Country Name", "measure": "Value", "aggregate": "sum"}
        labels = {"measure_label": "Population", "unit": "people", "category_label": "Country or region"}
    else:
        labels = {"measure_label": roles["measure"], "unit": "", "category_label": ""}
    with tempfile.TemporaryDirectory() as tmp:
        r = analyse(df, roles, labels, Path(tmp))
        hl = highlights(r["facts"], labels)
        nonfile = [c for c in r["charts"] if c["type"] != "table"]
        t(f"{name}: >=2 visual charts + table", len(nonfile) >= 2 and any(c["type"] == "table" for c in r["charts"]),
          [c["label"] for c in r["charts"]])
        t(f"{name}: category handling", (r["category_field"] is not None) == expect_cat)
        allowed = allowed_numbers(r["facts"])
        t(f"{name}: highlights pass own guard", all(not unsupported_numbers(h, allowed) for h in hl), [unsupported_numbers(h, allowed) for h in hl])
        tt = template_text(r["facts"], labels, {"source": "Test"})
        t(f"{name}: template passes guard", not unsupported_numbers(tt["analysis"] + tt["summary"], allowed | {2.0}))
        for c in r["charts"]:
            cols = set(__import__("pandas").read_csv(r["files"][c["file"]], nrows=1).columns)
            used = {v["field"] if isinstance(v, dict) else v for k, v in c.items() if k in ("x", "y", "color") and (isinstance(v, str) or "field" in v)}
            t(f"{name}: chart '{c['label'][:30]}' fields exist", used <= cols, (used, cols))
        p = cover(r, "Test title for cover", "Test source", Path(tmp) / "cover.png")
        from PIL import Image
        t(f"{name}: cover 1800x1013", Image.open(p).size == (1800, 1013))
        print("   ", name, r["time_period"], "|", hl[0])
# ZIP + OWID-style aggregates
from dv_acquire import unpack_zip
from dv_discovery import data_format
t("zip supported", data_format("https://ourworldindata.org/grapher/x.zip") == "zip")
with tempfile.TemporaryDirectory() as tmp:
    path, fmt, members, cite = unpack_zip(Path(sys.argv[3]) / "annual-area-burnt.zip", tmp)
    t("zip picks csv not metadata", path.endswith("annual-area-burnt.csv") and fmt == "csv", (path, members))
    t("zip citation", cite and cite.startswith("Global Wildfire"), cite)
    df, _ = parse(path, fmt)
    r = analyse(df, {"time": "Year", "category": "Entity", "measure": "Annual area burnt (ha)", "aggregate": "sum"},
                {"measure_label": "Area burnt", "unit": "ha", "category_label": "Country"}, Path(tmp))
    rem = r["facts"]["aggregate_groups_removed"]
    t("World and Africa removed", "World" in rem and "Africa" in rem, rem)
    t("no warning after removal", "warning" not in r["facts"], r["facts"].get("warning"))
# OWID non-ISO territories are not aggregates; "fell by 39.4%" matches -39.4
import pandas as pd
own = pd.DataFrame([{"Entity": e, "Code": c, "Year": y, "v": v} for y in (2020, 2021, 2022)
                    for e, c, v in (("Angola", "AGO", 30), ("Brazil", "BRA", 20), ("Chad", "TCD", 10), ("Kosovo", "OWID_KOS", 1),
                                    ("Asia", "", 40), ("Mystery region", "OWID_MYS", 55))])
with tempfile.TemporaryDirectory() as tmp:
    r = analyse(own, {"time": "Year", "category": "Entity", "measure": "v", "aggregate": "sum"}, {"measure_label": "Area"}, Path(tmp))
    rem = r["facts"]["aggregate_groups_removed"]
    t("Kosovo kept, Asia + large uncoded removed", "Kosovo" not in rem and "Asia" in rem and "Mystery region" in rem, rem)
t("sign-insensitive guard", unsupported_numbers("Burnt area fell by 39.4% since 2012.", {-39.4, 2012.0}) == [])
# Partial current year + label already containing "Total" (run 20261113T033727Z)
import pandas as pd, datetime
yr = datetime.date.today().year
rows = [{"Entity": e, "Code": c, "Year": y, "Area": v * (1 if y < yr else 0.4)}
        for y in range(yr - 14, yr + 1) for e, c, v in (("Angola", "AGO", 3e7), ("Brazil", "BRA", 2e7), ("Chad", "TCD", 1e7), ("World", "OWID_WRL", 6e7))]
with tempfile.TemporaryDirectory() as tmp:
    r = analyse(pd.DataFrame(rows), {"time": "Year", "category": "Entity", "measure": "Area", "aggregate": "sum"},
                {"measure_label": "Total annual area burnt by wildfires", "unit": "hectares", "category_label": "Country"}, Path(tmp))
    t("current year excluded", r["facts"]["incomplete_periods_removed"] == [str(yr)] and r["facts"]["time"]["last_period"] == str(yr - 1), r["facts"]["time"])
    hl = highlights(r["facts"], {"measure_label": "Total annual area burnt by wildfires"})
    t("no 'total total'", "total total" not in hl[0].lower() and "total annual area" in hl[0].lower(), hl[0])
    t("caption no double", all("Total total" not in c.get("caption", "") for c in r["charts"]), [c.get("caption") for c in r["charts"]])
    t("no false change from partial year", abs(r["facts"]["time"]["change_pct"]) < 1e-9, r["facts"]["time"]["change_pct"])
    cover(r, "Test", "Src", Path(tmp) / "c.png")
    print("   ", hl[0])
t("guard rejects invented", unsupported_numbers("rose by 43.7% to 999", {1.0, 2.5}) == [43.7, 999.0])
print("FAILURES:", fails); sys.exit(1 if fails else 0)
