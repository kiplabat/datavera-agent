"""Research-only lexical ranking. The catalogue and publisher are unchanged."""
import math
import re
from collections import Counter
from inventory_store import Inventory, normal

GENERIC = set('the and of in a to for data dataset datasets with from by global world worldwide time series year years monthly annual daily download csv original publisher open public licence license noaa nasa'.split())


def terms(text):
    """Normalise common plurals, retaining scientific abbreviations and numbers."""
    def stem(word):
        if len(word) > 5 and word.endswith('ies'):
            return word[:-3] + 'y'
        if len(word) > 4 and word.endswith('s') and not word.endswith(('ss', 'is', 'us')):
            return word[:-1]
        return word
    return {stem(t) for t in normal(text).split() if t not in GENERIC}


class ResearchInventory(Inventory):
    def search(self, query, limit=12):
        """Require shared topic terms; broad string resemblance is insufficient."""
        query_terms = terms(query)
        if not query_terms:
            query_terms = set(normal(query).split())
        if not query_terms:
            return []
        records = [(r, terms(' '.join(str(r.get(k) or '') for k in
                   ('title', 'source', 'source_url', 'geo_scope', 'geography', 'time_period'))))
                   for r in self.records.values()]
        df = Counter(t for _, words in records for t in words)
        weight = {t: math.log(1 + len(records) / (1 + df[t])) for t in query_terms}
        ranked = []
        for record, words in records:
            shared = query_terms & words
            phrase = normal(query) in normal(record.get('title'))
            required = min(2, len(query_terms))
            if len(shared) < required and not phrase:
                continue
            coverage = sum(weight[t] for t in shared) / max(1, sum(weight.values()))
            score = min(1.0, coverage + (0.15 if phrase else 0))
            ranked.append((score, record, sorted(shared)))
        ranked.sort(key=lambda row: (-row[0], row[1].get('title', '')))
        return [{'score': round(score, 3), 'matched_terms': shared,
                 **{k: r.get(k) for k in ('dataset_id', 'title', 'company', 'status', 'view_url', 'source_url', 'time_period')}}
                for score, r, shared in ranked[:max(1, min(int(limit), 30))]]
