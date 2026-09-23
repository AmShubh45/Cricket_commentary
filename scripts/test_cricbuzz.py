import urllib.request
import re
import json

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
url = "https://www.cricbuzz.com/live-cricket-scores/163061/ezone-vs-szone-final-duleep-trophy-2026"

req = urllib.request.Request(url, headers=headers)
with urllib.request.urlopen(req, timeout=10) as resp:
    html = resp.read().decode("utf-8")

# Find all self.__next_f.push calls
# Next.js app router pushes chunks like: self.__next_f.push([1,"..."])
# Let's extract the chunk that contains "commentaryPageData"
pattern = r'self\.__next_f\.push\(\[1,\s*"(.*?)"\]\)'
for match in re.finditer(r'self\.__next_f\.push\(\[1,\s*"(.*?)"\]\)', html, re.DOTALL):
    chunk_raw = match.group(1)
    if "commentaryPageData" in chunk_raw:
        print("Found chunk with commentaryPageData!")
        # Unescape the JSON string using json.loads
        try:
            unescaped = json.loads('"' + chunk_raw + '"')
            # Now inside unescaped, it has Next.js streaming format: e.g. 1a:{"commentaryPageData":...}
            # Find the JSON object starting from {"commentaryPageData"
            idx = unescaped.find('{"commentaryPageData"')
            if idx != -1:
                # parse the json object using raw_decode
                decoder = json.JSONDecoder()
                payload, end_idx = decoder.raw_decode(unescaped[idx:])
                comm_data = payload.get("commentaryPageData", {})
                match_comm = comm_data.get("matchCommentary", {})
                miniscore = comm_data.get("miniscore", {})
                
                print(f"\n--- MINISCORE KEYS ---")
                print(miniscore.keys())
                for k, v in miniscore.items():
                    if isinstance(v, dict):
                        print(f"  {k}: {list(v.keys())}")
                    else:
                        print(f"  {k}: {v}")
                
                print(f"\n--- BALL COMMENTARY ({len(match_comm)} balls) ---")
                # Sort balls chronologically by timestamp
                sorted_balls = sorted(match_comm.values(), key=lambda x: x.get("timestamp", 0))
                for b in sorted_balls[-6:]:
                    print(f"Over {b.get('ballMetric')}: {b.get('commText')}")
        except Exception as e:
            print("Error parsing chunk:", e)
        break
