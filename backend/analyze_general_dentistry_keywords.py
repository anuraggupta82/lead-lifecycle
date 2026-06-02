import json

with open("keywords_evaluation.json") as f:
    data = json.load(f)

gd_kws = [k for k in data if k["campaign"] == "General Dentistry New Landing Page (05/16 16:42)"]

ad_groups = {}
for kw in gd_kws:
    ag = kw["ad_group"]
    if ag not in ad_groups:
        ad_groups[ag] = {
            "total_kws": 0,
            "under_bid": 0,
            "rarely_served": 0,
            "avg_bid": 0.0,
            "bids": []
        }
    ad_groups[ag]["total_kws"] += 1
    if kw["under_bid"]:
        ad_groups[ag]["under_bid"] += 1
    if kw["serving_status"] == "RARELY_SERVED":
        ad_groups[ag]["rarely_served"] += 1
    if kw["bid"]:
        ad_groups[ag]["bids"].append(kw["bid"])

for ag, stats in ad_groups.items():
    stats["avg_bid"] = sum(stats["bids"]) / len(stats["bids"]) if stats["bids"] else 0.0
    print(f"Ad Group: {ag}")
    print(f"  Total Keywords: {stats['total_kws']}")
    print(f"  Under-bid Keywords: {stats['under_bid']} ({stats['under_bid']/stats['total_kws']:.1%})")
    print(f"  Average Bid: ${stats['avg_bid']:.2f}")
    print(f"  Rarely Served: {stats['rarely_served']}")
    
print("\nTop 5 highest first-page estimate keywords in General Dentistry:")
gd_kws.sort(key=lambda x: x["first_page_est"] or 0, reverse=True)
for k in gd_kws[:10]:
    print(f" - [{k['ad_group'][:15]}] {k['keyword']} ({k['match_type']}) | Bid: ${k['bid']} | First Page Est: ${k['first_page_est']} | Top of Page Est: ${k['top_of_page_est']}")
