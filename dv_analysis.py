"""Deterministic analysis: roles -> tidy chart files, computed facts, Datavera chart specs, cover image,
templated highlights/narrative, and a guard that rejects model text containing unsupported numbers."""
import json
import math
import re
from datetime import date

import pandas as pd

from dv_acquire import is_year_series

PALETTE = ["#E8633A", "#D4A853", "#4A8C6F", "#4A7FA8", "#8A6A9E", "#C0392B"]
INK, INK_SOFT, SURFACE, BORDER = "#1A1A1A", "#8A7F76", "#F7F5F2", "#E2DDD8"


class AnalysisError(RuntimeError):
    pass


def snake(name, fallback="value"):
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(name)).strip("_").lower()
    s = re.sub(r"_+", "_", s)[:48]
    if not s or s[0].isdigit():
        s = (fallback + "_" + s).strip("_")
    return s


def fmt(x, unit=""):
    """Human formatting used in every published sentence (and accepted by the number guard)."""
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "n/a"
    a = abs(x)
    if a >= 1e9:
        s = f"{x/1e9:.2f}".rstrip("0").rstrip(".") + " billion"
    elif a >= 1e6:
        s = f"{x/1e6:.2f}".rstrip("0").rstrip(".") + " million"
    elif a >= 1000:
        s = f"{x:,.0f}"
    elif a >= 100:
        s = f"{x:.1f}".rstrip("0").rstrip(".")
    elif a >= 1:
        s = f"{x:.2f}".rstrip("0").rstrip(".")
    elif a == 0:
        s = "0"
    else:
        s = f"{x:.3g}"
    return (s + (" " + unit if unit else "")).strip()


def with_agg(word, label):
    """'Total' + label, unless the label already says total/average/mean/median."""
    if re.match(r"(?i)\s*(total|average|mean|median|sum|cumulative)\b", label):
        return label[0].upper() + label[1:]
    return f"{word} {label[0].lower() + label[1:]}"


def pct(x):
    return "n/a" if x is None or not math.isfinite(x) else f"{x:.1f}%"


# --------------------------------------------------------------------------- roles

def candidate_roles(prof):
    cols = prof["columns"]
    time = [c["name"] for c in cols if c["kind"] in ("year", "date")]
    time += [c["name"] for c in cols if c["kind"] == "number" and re.search(r"(?i)^(year|yr|fiscal.?year)$", c["name"])
             and c["name"] not in time]
    idlike = lambda c: re.search(r"(?i)(^id$|_id$|\bid\b|\bcode$|^fips|^iso\b|\bzip\b|postcode|^(lat|lon|lng|latitude|longitude)$)", c["name"])
    measures = [c["name"] for c in cols if c["kind"] == "number" and c["name"] not in time and not idlike(c)
                and c["non_null"] >= max(5, 0.5 * prof["rows"]) and c["distinct"] > 1]
    # Identifier columns (ISO codes etc.) are never the grouping; the readable name column is.
    codeish = lambda c: re.fullmatch(r"(?i)(code|iso.?3?|iso.?code|country.?code|iso3166.*|.*_code|.*\bcode)", c["name"]) \
        or all(re.fullmatch(r"[A-Z0-9_]{2,9}", str(e)) for e in c["examples"])
    cats = [c["name"] for c in cols if c["kind"] == "text" and 2 <= c["distinct"] <= 400
            and c["non_null"] >= 0.5 * prof["rows"] and not codeish(c)]
    return {"time": time, "measure": measures, "category": cats}


def heuristic_roles(prof):
    cands = candidate_roles(prof)
    if not cands["measure"]:
        raise AnalysisError("no usable numeric measure column")
    cats = sorted(cands["category"], key=lambda n: next(c["distinct"] for c in prof["columns"] if c["name"] == n))
    # Prefer a category with a moderate number of groups (countries, regions, types).
    cat = next((n for n in cats if 3 <= next(c["distinct"] for c in prof["columns"] if c["name"] == n) <= 250), None)
    return {"time": cands["time"][0] if cands["time"] else None, "category": cat,
            "measure": cands["measure"][0], "aggregate": "sum"}


AGGREGATE_NAMES = re.compile(
    r"(?i)^(world|total|all|global|overall|grand total|sum|average|other|africa|asia|europe|oceania|americas|"
    r"south america|north america|antarctica|european union(?: \(27\))?)$"
    r"|\b(total|income|world|members|ida|ibrd|oecd|euro area|european union|small states|fragile|dividend|"
    r"heavily indebted|least developed|excluding|excl\.|sub-saharan|middle east|north america|latin america|"
    r"south asia|central asia|arab world|not classified|africa eastern|africa western|central europe and the baltics)\b"
    r"|&\s*(pacific|caribbean|central asia|north africa)\b")


def code_column(df):
    return next((c for c in df.columns if re.fullmatch(r"(?i)(code|iso.?3?|iso.?code|country.?code|iso3166.*)", str(c))), None)


def drop_aggregate_groups(d, cat, codes=None, value=None):
    """Remove rows that are totals/regional aggregates of the other groups (WDI- and OWID-style files).
    A row without a plain ISO-3 code (blank, OWID_*) is an aggregate only if it is also bigger than every
    coded group: Our World in Data gives small territories such as Kosovo (OWID_KOS) non-ISO codes too."""
    names = d[cat].astype(str)
    mask = names.str.contains(AGGREGATE_NAMES)
    if codes is not None and value is not None:
        valid = codes.astype(str).str.fullmatch(r"[A-Z]{3}")
        if valid.sum() >= 3 and (~valid).any():
            totals = d.groupby(names)[value].sum()
            largest_coded = totals[names[valid].unique()].max()
            big_uncoded = [n for n in names[~valid].unique() if totals[n] > largest_coded]
            mask = mask | (~valid & names.isin(big_uncoded))
    if mask.any() and (~mask).sum() >= 3:
        return d[~mask], sorted(names[mask].unique().tolist())
    return d, []


def check_roles(df, roles):
    if roles.get("measure") not in df.columns:
        raise AnalysisError("measure column missing")
    if not pd.api.types.is_numeric_dtype(df[roles["measure"]]):
        raise AnalysisError("measure is not numeric")
    if not roles.get("time") and not roles.get("category"):
        raise AnalysisError("need a time or category column")
    for key in ("time", "category"):
        if roles.get(key) and roles[key] not in df.columns:
            raise AnalysisError(f"{key} column missing")
    if roles.get("time") == roles.get("category") and roles.get("time"):
        roles["category"] = None
    if roles.get("aggregate") not in ("sum", "mean"):
        roles["aggregate"] = "sum"
    return roles


# --------------------------------------------------------------------------- analysis

def _period(df, col):
    s = df[col]
    if is_year_series(s):
        return pd.to_numeric(s, errors="coerce").astype("Int64"), "year"
    dt = pd.to_datetime(s.astype(str), errors="coerce", format="mixed")
    if dt.notna().mean() < 0.9:
        raise AnalysisError("time column does not parse as dates/years")
    if dt.dt.year.nunique() >= 5:
        return dt.dt.year.astype("Int64"), "year"
    return dt.dt.to_period("M").dt.to_timestamp().dt.strftime("%Y-%m-01"), "month"


def _trend(series):
    """Least-squares slope per period of an overall series indexed by year/month order."""
    y = series.astype(float).values
    n = len(y)
    if n < 3:
        return None
    x = list(range(n))
    mx, my = sum(x) / n, sum(y) / n
    den = sum((xi - mx) ** 2 for xi in x)
    return sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / den if den else None


def analyse(df, roles, labels, folder):
    """Write chart CSVs into folder and return {files, facts, charts, highlights, time_period, ...}."""
    roles = check_roles(df, dict(roles))
    m, t, c, agg = roles["measure"], roles.get("time"), roles.get("category"), roles["aggregate"]
    m_label = labels.get("measure_label") or m
    unit = labels.get("unit") or ""
    c_label = labels.get("category_label") or (c or "")
    value = snake(m_label if labels.get("measure_label") else m)
    cat = snake(c, "group") if c else None
    if cat == value:
        cat = cat + "_group"
    d = pd.DataFrame({value: pd.to_numeric(df[m], errors="coerce")})
    if c:
        d[cat] = df[c].astype(str).str.strip()
    period_kind = None
    if t:
        d["period"], period_kind = _period(df, t)
    before = len(d)
    d = d.dropna()
    if c:
        d = d[d[cat].str.len() > 0]
    dropped = before - len(d)
    removed_groups = []
    if c:
        cc = code_column(df)
        d, removed_groups = drop_aggregate_groups(d, cat, df.loc[d.index, cc] if cc and cc != c else None, value)
    incomplete, future = [], []
    if t:
        today = date.today()
        if period_kind == "year":
            yrs = d["period"].astype(int)
            incomplete = sorted({int(y) for y in yrs[yrs == today.year]})
            future = sorted({int(y) for y in yrs[yrs > today.year]})
            d = d[yrs != today.year]  # the current year is only partly observed
        else:
            this_month = today.strftime("%Y-%m-01")
            incomplete = sorted(set(d.loc[d["period"] == this_month, "period"]))
            future = sorted(set(d.loc[d["period"] > this_month, "period"]))
            d = d[d["period"] != this_month]
    if len(d) < 8:
        raise AnalysisError(f"only {len(d)} usable rows after cleaning")
    tname = "year" if period_kind == "year" else "date"
    if t:
        d = d.rename(columns={"period": tname})
    reducer = "sum" if agg == "sum" else "mean"
    files, charts, facts = {}, [], {"rows_used": int(len(d)), "rows_dropped": int(dropped),
                                    "aggregate": agg, "measure": m_label, "unit": unit,
                                    "aggregate_groups_removed": removed_groups[:40],
                                    "incomplete_periods_removed": [str(x) for x in incomplete]}
    if future:
        facts["warning"] = (f"The file contains periods after today ({', '.join(map(str, future[:5]))}); they are probably "
                            "projections. Check the source before publishing them as observations.")
    yfmt = {"format": "~s"} if d[value].abs().max() >= 10000 else {}
    ytitle = (m_label + (f" ({unit})" if unit and unit not in m_label else ""))[:40]

    single = bool(t) and not c and int(d.groupby(tname).size().max()) == 1
    facts["one_row_per_period"] = single
    word = "" if single else ("Total " if agg == "sum" else "Average ")
    if t:
        overall = d.groupby(tname)[value].agg(reducer).sort_index()
        overall = overall[overall.notna()]
        if len(overall) < 3:
            raise AnalysisError("fewer than three time periods")
        first_p, last_p = overall.index[0], overall.index[-1]
        first_v, last_v = float(overall.iloc[0]), float(overall.iloc[-1])
        facts["time"] = {
            "first_period": str(first_p), "last_period": str(last_p), "periods": int(len(overall)),
            "first_value": first_v, "last_value": last_v,
            "change": last_v - first_v,
            "change_pct": (last_v - first_v) / abs(first_v) * 100 if first_v else None,
            "max_period": str(overall.idxmax()), "max_value": float(overall.max()),
            "min_period": str(overall.idxmin()), "min_value": float(overall.min()),
            "trend_per_period": _trend(overall),
        }
        time_period = f"{first_p}–{last_p}" if period_kind == "year" else f"{str(first_p)[:7]}–{str(last_p)[:7]}"
        xspec = ({"field": "year", "type": "quantitative", "format": "d", "title": "Year", "scale": {"zero": False}}
                 if period_kind == "year" else {"field": "date", "type": "temporal", "title": "Month"})
        if c:
            totals = d.groupby(cat)[value].agg(reducer).sort_values(ascending=False)
            top = list(totals.index[:5])
            s = d.copy()
            if agg == "sum" and d[cat].nunique() > 5:
                s[cat] = s[cat].where(s[cat].isin(top), "Other")
            else:
                s = s[s[cat].isin(top)]
            series = s.groupby([tname, cat])[value].agg(reducer).reset_index()
            files["series"] = series
            charts.append({"type": "chart", "file": "series", "chart_type": "line", "mark": "line",
                           "label": f"{m_label} over time: top five {(c_label or 'group').lower()} groups"[:80],
                           "caption": f"{with_agg('Total' if agg == 'sum' else 'Average', m_label)} per {tname} for the five "
                                      f"largest {(c_label or 'group').lower()} groups"
                                      + (", with the rest combined as Other." if agg == "sum" and d[cat].nunique() > 5 else "."),
                           "x": xspec, "y": {"field": value, "title": ytitle, **yfmt}, "color": {"field": cat, "title": c_label or cat},
                           "tooltip": [tname, cat, value]})
            latest = d[d[tname] == last_p]
            if latest[cat].nunique() < 3:
                latest = d
                rank_scope = f"all periods ({time_period})"
            else:
                rank_scope = str(last_p)
            ranking = latest.groupby(cat)[value].agg(reducer).sort_values(ascending=False).reset_index()
            facts["ranking_scope"] = rank_scope
        else:
            series = overall.reset_index().rename(columns={overall.name: value} if overall.name != value else {})
            files["series"] = series
            charts.append({"type": "chart", "file": "series", "chart_type": "line", "mark": "line",
                           "label": f"{m_label} over time"[:80],
                           "caption": (f"{with_agg(word.strip(), m_label)} per {tname}, {time_period}." if word
                                      else f"{m_label} for each {tname}, {time_period}."),
                           "x": xspec, "y": {"field": value, "title": ytitle, **yfmt}, "tooltip": [tname, value]})
            ranking = None
            if period_kind == "year" and int(last_p) - int(first_p) >= 20:
                dec = overall.reset_index()
                dec["decade"] = (dec["year"].astype(int) // 10 * 10).astype(str) + "s"
                decade = dec.groupby("decade")[value].mean().reset_index()
                files["decades"] = decade
                facts["decades"] = {r["decade"]: float(r[value]) for _, r in decade.iterrows()}
                charts.append({"type": "chart", "file": "decades", "chart_type": "bar", "mark": "bar",
                               "label": f"Average {m_label.lower()} by decade"[:80],
                               "caption": "Mean of the yearly values within each decade.",
                               "x": {"field": "decade", "type": "ordinal", "title": "Decade"},
                               "y": {"field": value, "title": ytitle, **yfmt}, "tooltip": ["decade", value]})
            else:
                ch = overall.diff().dropna().reset_index()
                ch.columns = [tname, "change"]
                files["changes"] = ch
                charts.append({"type": "chart", "file": "changes", "chart_type": "bar", "mark": "bar",
                               "label": f"Change in {m_label.lower()} from the previous {tname}"[:80],
                               "caption": "Positive bars are increases, negative bars decreases.",
                               "x": (dict(xspec, type="ordinal") if period_kind == "year" else xspec),
                               "y": {"field": "change", "title": "Change", **yfmt}, "tooltip": [tname, "change"]})
            charts.append({"type": "table", "file": "series", "label": f"{m_label} by {tname}"[:80],
                           "columns": [{"field": tname, "label": tname.title()}, {"field": value, "label": ytitle}],
                           "sort": {"field": tname, "order": "desc"}, "limit": 200})
    else:
        time_period = labels.get("time_period_hint") or ""
        ranking = d.groupby(cat)[value].agg(reducer).sort_values(ascending=False).reset_index()
        facts["ranking_scope"] = "all rows"
        files["distribution"] = d[[cat, value]]
        dist = d[value]
        facts["distribution"] = {"median": float(dist.median()), "p10": float(dist.quantile(.1)),
                                 "p90": float(dist.quantile(.9)), "count": int(dist.count())}

    if ranking is not None:
        ranking = ranking[ranking[value].notna()]
        n_groups = int(len(ranking))
        top = ranking.head(15)
        files["ranking"] = ranking
        total = float(ranking[value].sum())
        shares_ok = agg == "sum" and (ranking[value] >= 0).all() and total > 0
        facts["ranking"] = {
            "groups": n_groups,
            "top": [{"name": str(r[cat]), "value": float(r[value]),
                     "share_pct": float(r[value] / total * 100) if shares_ok else None}
                    for _, r in ranking.head(5).iterrows()],
            "bottom": {"name": str(ranking.iloc[-1][cat]), "value": float(ranking.iloc[-1][value])},
            "total": total if shares_ok else None,
            "median_group_value": float(ranking[value].median()),
        }
        if shares_ok and n_groups > 3 and float(ranking[value].iloc[0]) > 0.5 * total:
            facts["warning"] = (f"'{ranking.iloc[0][cat]}' holds over half of the total; it may be a total or "
                                "aggregate row. Review the ranking before publishing.")
        charts.insert(0 if not t else 1, {
            "type": "chart", "file": "ranking", "chart_type": "bar", "mark": "bar",
            "label": f"{c_label or cat}: ranked by {m_label.lower()}"[:80],
            "caption": f"{with_agg('Total' if agg == 'sum' else 'Average', m_label)} by {c_label.lower() or cat}, "
                       f"{facts['ranking_scope']}; top {min(15, n_groups)} of {n_groups} shown.",
            "y": {"field": cat, "type": "nominal", "sort": "-x", "title": (c_label or cat)[:30]},
            "x": {"field": value, "title": ytitle, **yfmt}, "tooltip": [cat, value],
            "filter": [{"field": value, "op": ">=", "value": float(top[value].min())}] if n_groups > 15 else None,
            "height": 420})
        charts.append({"type": "table", "file": "ranking", "label": f"{m_label} by {c_label or cat}"[:80],
                       "columns": [{"field": cat, "label": c_label or cat}, {"field": value, "label": ytitle}],
                       "sort": {"field": value, "order": "desc"}, "limit": 100})
        if not t:
            charts.insert(1, {"type": "chart", "file": "distribution", "chart_type": "histogram", "mark": "bar",
                              "label": f"How {m_label.lower()} is distributed"[:80],
                              "caption": "Number of rows in each value band.",
                              "x": {"field": value, "bin": {"maxbins": 30}, "title": ytitle},
                              "y": {"aggregate": "count", "title": "Rows"}})
    for ch in charts:
        if ch.get("filter") is None:
            ch.pop("filter", None)
    paths = {}
    for key, frame in files.items():
        frame = frame.head(50000)
        path = folder / f"{key}.csv"
        frame.to_csv(path, index=False)
        paths[key] = str(path)
    facts["time_period"] = time_period
    return {"files": paths, "facts": facts, "charts": charts, "time_period": time_period,
            "value_field": value, "category_field": cat, "time_field": tname if t else None,
            "period_kind": period_kind, "roles": roles}


# --------------------------------------------------------------------------- text

def highlights(facts, labels):
    m = labels.get("measure_label") or facts["measure"]
    unit = facts.get("unit") or ""
    out = []
    tf = facts.get("time")
    if tf:
        word = "" if facts.get("one_row_per_period") else ("total" if facts["aggregate"] == "sum" else "average")
        subject = ("The " + with_agg(word, m)[0].lower() + with_agg(word, m)[1:]) if word else m
        out.append(f"{subject} went from {fmt(tf['first_value'], unit)} in {tf['first_period']} "
                   f"to {fmt(tf['last_value'], unit)} in {tf['last_period']}"
                   + (f", a change of {pct(tf['change_pct'])}." if tf.get("change_pct") is not None and abs(tf['first_value']) > 1e-9 else "."))
        out.append(f"The highest value was {fmt(tf['max_value'], unit)} in {tf['max_period']}, "
                   f"and the lowest {fmt(tf['min_value'], unit)} in {tf['min_period']}.")
    rk = facts.get("ranking")
    if rk and rk["top"]:
        lead = rk["top"][0]
        share = f" ({pct(lead['share_pct'])} of the total)" if lead.get("share_pct") is not None else ""
        out.append(f"{lead['name']} ranks first with {fmt(lead['value'], unit)}{share} across {rk['groups']} "
                   f"groups ({facts.get('ranking_scope')}).")
        if len(rk["top"]) >= 3:
            names = ", ".join(r["name"] for r in rk["top"][1:3])
            out.append(f"{names} follow in second and third place.")
        out.append(f"The median group value is {fmt(rk['median_group_value'], unit)}; the lowest is "
                   f"{rk['bottom']['name']} at {fmt(rk['bottom']['value'], unit)}.")
    dist = facts.get("distribution")
    if dist:
        out.append(f"The median row value is {fmt(dist['median'], unit)}; 80% of the {dist['count']} rows fall "
                   f"between {fmt(dist['p10'], unit)} and {fmt(dist['p90'], unit)}.")
    return [h[:199] for h in out[:6]]


def template_text(facts, labels, meta):
    m = labels.get("measure_label") or facts["measure"]
    hl = highlights(facts, labels)
    period = facts.get("time_period")
    title = f"{m}{(' by ' + labels['category_label']) if labels.get('category_label') and facts.get('ranking') else ''}"
    title = (title + (f", {period}" if period else ""))[:100]
    summary = (f"{m} from {meta['source']}" + (f", {period}" if period else "") + ". " + (hl[0] if hl else "")).strip()[:399]
    removed = facts.get("aggregate_groups_removed") or []
    analysis = "\n\n".join([
        f"This dataset presents {m.lower()} published by {meta['source']}" + (f" for {period}" if period else "") +
        f". It uses {fmt(facts['rows_used'])} rows from the original file.",
        " ".join(hl),
        ("Rows that are totals or regional aggregates of other rows were excluded before ranking. " if removed else "") +
        (f"The current, incomplete period ({', '.join(facts['incomplete_periods_removed'])}) was excluded. "
         if facts.get("incomplete_periods_removed") else "") +
        "The figures are computed directly from the downloaded file without modelling or adjustment. "
        "Coverage, definitions and any revisions are as documented by the original publisher; consult the source "
        "page before drawing conclusions beyond what the charts show.",
    ])
    return {"title": title, "subtitle": "", "summary": summary, "analysis": analysis}


NUM_RE = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def on_topic(text, terms):
    """True if the text shares at least one meaningful word with the dataset's own vocabulary."""
    words = {w for w in re.findall(r"[a-z]{4,}", str(text).lower())}
    return bool(words & terms)


def numbers_in(text):
    out = []
    for tok in NUM_RE.findall(str(text)):
        try:
            out.append(float(tok.replace(",", "")))
        except ValueError:
            pass
    return out


def allowed_numbers(facts, extra_text=""):
    allowed = set()

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            allowed.add(float(v))
            for s in (fmt(v), pct(v)):
                allowed.update(numbers_in(s))
        elif isinstance(v, str):
            allowed.update(numbers_in(v))
    walk(facts)
    allowed.update(numbers_in(extra_text))
    return allowed


def unsupported_numbers(text, allowed):
    bad = []
    for n in numbers_in(text):
        if n == int(n) and 0 <= n <= 12:
            continue  # small counts and ordinals ("top 5", "two decades")
        # Sign-insensitive: "fell by 39.4%" states the computed change of -39.4%.
        if any(abs(abs(n) - abs(a)) <= max(0.051, abs(a) * 0.005) for a in allowed):
            continue
        bad.append(n)
    return bad


# --------------------------------------------------------------------------- cover

def cover(result, title, source, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    files = result["files"]
    fig = plt.figure(figsize=(18, 10.13), dpi=100, facecolor=SURFACE)
    fig.text(0.06, 0.90, title[:70] + ("…" if len(title) > 70 else ""), fontsize=34, fontweight="bold", color=INK)
    fig.text(0.06, 0.05, f"Source: {source[:90]} · datavera.io", fontsize=16, color=INK_SOFT)
    ax = fig.add_axes([0.07, 0.15, 0.88, 0.66], facecolor=SURFACE)
    first = result["charts"][0]
    frame = pd.read_csv(files[first["file"]])
    v = result["value_field"]
    if first["mark"] == "line":
        tf = result["time_field"]
        if result["category_field"] and result["category_field"] in frame:
            for i, (name, g) in enumerate(frame.groupby(result["category_field"])):
                x = g[tf] if tf == "year" else pd.to_datetime(g[tf])
                ax.plot(x, g[v], color=PALETTE[i % 6], linewidth=4, label=str(name)[:30])
            ax.legend(frameon=False, fontsize=16, loc="upper left", ncol=3)
        else:
            x = frame[tf] if tf == "year" else pd.to_datetime(frame[tf])
            ax.plot(x, frame[v], color=PALETTE[0], linewidth=5)
    else:
        cat = result["category_field"]
        top = frame.sort_values(v, ascending=False).head(8).iloc[::-1]
        ax.barh(top[cat].astype(str).str.slice(0, 32), top[v], color=PALETTE[0])
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(BORDER)
    from matplotlib.ticker import FuncFormatter

    def short(v, _pos=None):
        a = abs(v)
        for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
            if a >= div:
                return f"{v / div:.3g}{suf}"
        return f"{v:.3g}"
    (ax.yaxis if first["mark"] == "line" else ax.xaxis).set_major_formatter(FuncFormatter(short))
    ax.tick_params(colors=INK_SOFT, labelsize=16)
    ax.grid(axis="y" if first["mark"] == "line" else "x", color=BORDER, linewidth=1)
    ax.set_axisbelow(True)
    fig.savefig(path, dpi=100, facecolor=SURFACE)
    plt.close(fig)
    from PIL import Image
    with Image.open(path) as im:
        if im.size != (1800, 1013):
            im.resize((1800, 1013)).save(path)
    return path
