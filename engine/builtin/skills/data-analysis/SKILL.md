---
name: data-analysis
description: Use when analysing a data file (CSV, Excel, JSON) or a downloaded dataset: profiling, cleaning, statistics, charts.
---

# Data analysis with run_python

1. **Profile** (one run_python call): load the file with pandas; print shape, dtypes, `head()`, missing values
   per column, and unique counts for text columns. For CSVs with odd encodings try `encoding="latin-1"`;
   for files with notes above the header use `skiprows`.
2. **Understand**: identify the time column, the grouping columns (country, category…) and the measures.
   Note units. Check for aggregate rows (World, Total, regions, income groups) and an incomplete latest
   period (current year/month) — exclude them from rankings and changes, and say so.
3. **Compute**: totals or means as appropriate (sums for counts and amounts, means for rates and prices),
   changes between the first and last complete periods, top/bottom groups. Print the numbers.
4. **Chart**: matplotlib, one idea per chart, labelled axes with units, title, `plt.tight_layout()`,
   `savefig("chart_name.png", dpi=150)`. Line charts for time, horizontal bars for rankings.
5. **Report**: what the data is and where it came from, 3-5 findings with the exact numbers printed above,
   caveats (exclusions, gaps), and the full paths of files produced.

Never report a number that was not printed by your code.
