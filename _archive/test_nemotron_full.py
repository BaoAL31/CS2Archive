import os, json
from dotenv import load_dotenv
load_dotenv('.env')
from openai import OpenAI

client = OpenAI(base_url='https://openrouter.ai/api/v1', api_key=os.getenv('OPENROUTER_API_KEY'))

kt = json.load(open('renders/hl-team_teses vs team_SVNONETHREE - cache/action_timeline.json'))

pro_sids = {'76561198044045107', '76561198034202275', '76561197996678278', '76561198838822582', '76561198920720017'}
players = {}
for k in kt['kills']:
    if k['attacker_steam_id']: players[k['attacker_steam_id']] = k['attacker']
    if k['victim_steam_id']: players[k['victim_steam_id']] = k['victim']

pro_marks = {sid: ' (PRO)' for sid in pro_sids if sid in players}
kills_summary = []
for i, k in enumerate(kt['kills']):
    atk_pro = ' (PRO)' if k['attacker_steam_id'] in pro_sids else ''
    vic_pro = ' (PRO)' if k['victim_steam_id'] in pro_sids else ''
    kills_summary.append('  [{}] tick={} rnd={} {} -> {} ({}{}{})'.format(i, k['tick'], k['round'], k['attacker']+atk_pro, k['victim']+vic_pro, k['weapon'], ' HS' if k['headshot'] else '', ' BOMB' if k['is_bomb'] else ''))
player_list = '\n'.join('  {}: {}{}'.format(sid, name, pro_marks.get(sid, '')) for sid, name in players.items())

prompt = 'You are an expert CS2 highlight reel editor. Watch this match through its Kill Timeline and decide which player\'s POV tells the best story at each moment.\n\nMATCH: ' + kt['map'] + ' (' + kt['source'] + ')\nKILLS: ' + str(kt['kill_count']) + '\n\nPLAYERS (steam_id: name):\n' + player_list + '\n\nKILL TIMELINE (index, tick, round, attacker -> victim):\n' + '\n'.join(kills_summary) + '\n\nRULES:\n1. Output segments covering FULL match duration, non-overlapping, sequential.\n2. Segment boundaries align to kill ticks. Min 1 kill per segment.\n3. Types: multi_kill (2+ kills same attacker quick succession), entry (first kill of round), clutch (1vX won), trade (teammate kill within 3s), utility (utility-defined play), default (fallback).\n4. POV priority:\n   - multi_kill/entry: attacker POV\n   - clutch: solo winner POV\n   - trade: trade-killer POV\n   - utility: utility thrower POV\n   - default: most recent killer POV\n5. Recognised Pros (marked PRO) get POV priority when involved. Unknowns only if no PRO in segment.\n6. Victim POV ONLY for clutches (clutcher\'s perspective). Never for multi_kill/entry/trade.\n7. Every kill_index must reference input Kill Timeline array (0-based).\n8. Rationale: 1-2 sentences explaining POV choice.\n\nOUTPUT: JSON ONLY matching schema:\n{\n  "demo_path": "' + kt['demo_path'] + '",\n  "map": "' + kt['map'] + '",\n  "segments": [\n    {"start_tick": 12345, "end_tick": 15678, "pov_steam_id": "7656119...", "segment_type": "multi_kill", "kill_indices": [0,1], "rationale": "..."}\n  ]\n}'

print('Prompt len:', len(prompt))
resp = client.chat.completions.create(
    model='nvidia/nemotron-3-ultra-550b-a55b:free',
    messages=[{'role': 'system', 'content': 'Output ONLY valid JSON. No markdown, no commentary.'}, {'role': 'user', 'content': prompt}],
    temperature=0.1,
    response_format={'type': 'json_object'},
    max_tokens=8000,
)
print('Content:', resp.choices[0].message.content[:500])