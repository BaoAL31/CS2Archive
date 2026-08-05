import urllib.request, json, re

url = 'https://www.youtube.com/watch?v=RB2S46JZ0V0'
req = urllib.request.Request(url, headers={
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
})
with urllib.request.urlopen(req, timeout=10) as resp:
    html = resp.read().decode('utf-8')

m = re.search(r'var ytInitialPlayerResponse\s*=\s*({.+?});', html, re.DOTALL)
if m:
    data = json.loads(m.group(1))
    desc = data.get('videoDetails', {}).get('description', '') or \
           data.get('videoDetails', {}).get('shortDescription', '')
    print("Full description:")
    print(repr(desc))
    print()
    # Show each char with its codepoint 
    print("Unicode codepoints:")
    for i, c in enumerate(desc):
        if ord(c) > 127:
            print(f"  pos {i}: U+{ord(c):04X} = {c!r}")
    # Also check the replacement chars
    for i, c in enumerate(desc):
        if ord(c) == 0xFFFD:
            # Look at surrounding context
            start = max(0, i-10)
            end = min(len(desc), i+10)
            print(f"  Replacement at pos {i}: context = {repr(desc[start:end])}")
            print(f"  Bytes around replacement area in orig desc:")
            # Show hex of the raw substring that contains it
            substr = desc[start:end]
            print(f"  Hex of surrounding string: {substr.encode('utf-8', errors='replace').hex(' ')}")
