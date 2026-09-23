import urllib.request
import json
import re

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
url = 'https://www.cricbuzz.com/live-cricket-scores/171181'

req = urllib.request.Request(url, headers=headers)
with urllib.request.urlopen(req, timeout=10) as resp:
    html = resp.read().decode('utf-8')

print('HTML length:', len(html))

# Find title or metadata
m_title = re.search(r'<title>(.*?)</title>', html)
if m_title:
    print('Title:', m_title.group(1))

m_desc = re.search(r'<meta name="description" content="(.*?)"', html)
if m_desc:
    print('Description:', m_desc.group(1))

# Find commentaryPageData chunk
for match in re.finditer(r'self\.__next_f\.push\(\[1,\s*"(.*?)"\]\)', html, re.DOTALL):
    chunk_raw = match.group(1)
    if 'commentaryPageData' in chunk_raw:
        try:
            unescaped = json.loads('"' + chunk_raw + '"')
            idx = unescaped.find('{"commentaryPageData"')
            if idx != -1:
                decoder = json.JSONDecoder()
                payload, _ = decoder.raw_decode(unescaped[idx:])
                page_data = payload.get('commentaryPageData', {})
                print('commentaryPageData keys:', list(page_data.keys()))
                print('miniscore value:', page_data.get('miniscore'))
                print('matchHeader:', page_data.get('matchHeader'))
                print('customStatus:', page_data.get('customStatus'))
                break
        except Exception as e:
            print('Decode error:', e)
