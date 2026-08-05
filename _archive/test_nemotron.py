import os, json
from dotenv import load_dotenv
load_dotenv('.env')
from openai import OpenAI

client = OpenAI(base_url='https://openrouter.ai/api/v1', api_key=os.getenv('OPENROUTER_API_KEY'))

kt = json.load(open('renders/hl-team_teses vs team_SVNONETHREE - cache/action_timeline.json'))

pro_sids = {'76561198044045107', '76561198034202275', '76561197996678278', '76561198838822582', '76561198920720017'}
players = {}
for k in kt['kills'][:20]:
    if k['attacker_steam_id']: players[k['attacker_steam_id']] = k['attacker']
    if k['victim_steam_id']: players[k['victim_steam_id']] = k['victim']

pro_marks = {sid: ' (PRO)' for sid in pro_sids if sid in players}
kills_summary = []
for i, k in enumerate(kt['kills'][:20]):
    atk_pro = ' (PRO)' if k['attacker_steam_id'] in pro_sids else ''
    vic_pro = ' (PRO)' if k['victim_steam_id'] in pro_sids else ''
    kills_summary.append('  [{}] tick={} rnd={} {} -> {} ({}{}{})'.format(i, k['tick'], k['round'], k['attacker']+atk_pro, k['victim']+vic_pro, k['weapon'], ' HS' if k['headshot'] else '', ' BOMB' if k['is_bomb'] else ''))
player_list = '\n'.join('  {}: {}{}'.format(sid, name, pro_marks.get(sid, '')) for sid, name in players.items())

prompt = 'You are a CS2 highlight editor. Output ONLY JSON.\n\nMATCH: ' + kt['map'] + '\nKILLS: 20 (subset)\n\nPLAYERS:\n' + player_list + '\n\nKILLS:\n' + '\n'.join(kills_summary) + '\n\nOUTPUT JSON:\n{"segments": [{"start_tick": 1000, "end_tick": 2000, "pov_steam_id": "...", "segment_type": "entry", "kill_indices": [0], "rationale": "..."}]}'

print('Prompt len:', len(prompt))
resp = client.chat.completions.create(
    model='nvidia/nemotron-3-ultra-550b-a55b:free',
    messages=[{'role': 'system', 'content': 'Output ONLY valid JSON. No reasoning, no commentary, no explanation.'}, {'role': 'user', 'content': prompt}],
    temperature=0.1,
    response_format={'type': 'json_object'},
    max_tokens=2000,
)
print('Content:', resp.choices[0].message.content[:500])