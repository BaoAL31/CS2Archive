import urllib.request, urllib.error, re, json

url = 'https://www.youtube.com/watch?v=RB2S46JZ0V0'
req = urllib.request.Request(url, headers={
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
})
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        html = resp.read().decode('utf-8')
    # Find ytInitialPlayerResponse JSON
    m = re.search(r'var ytInitialPlayerResponse\s*=\s*({.+?});', html, re.DOTALL)
    if m:
        data = json.loads(m.group(1))
        desc = data.get('videoDetails', {}).get('shortDescription', '') or \
               data.get('videoDetails', {}).get('description', '')
        print('Description from YouTube:')
        print(repr(desc[:300]))
        print()
        print('Rendered:')
        print(desc[:500])
    else:
        # Try alternative: find any description field
        descs = re.findall(r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
        for d in descs[:3]:
            print('Found description (raw json):', repr(d[:300]))
except Exception as e:
    print('Error:', e)
