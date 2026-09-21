// Execute the actual adapted upstream runtime against a tiny Panorama surface.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
let players = [{xuid:'111',slot:1}, {xuid:'222',slot:2}, {xuid:'999',slot:3}];
const commands = [], mutes = [];
const panel = () => ({visible:true, SetHasClass(){}, FindChildTraverse(){return null;}});
const dock = panel(), toggle = panel();
const context = { FindChildTraverse(id) {
  return {SwiftDemoVoiceDock:dock,SwiftDemoVoiceMenuToggle:toggle}[id] || null;
}, IsPlayingDemo(){return true;} };
const sandbox = {
  $: {Schedule(){}, Msg(){}, GetContextPanel(){return context;}, Localize(s){return s;}},
  GameInterfaceAPI: {ConsoleCommand(s){commands.push(s);}},
  GameStateAPI: {
    GetPlayerDataJSO(){return {players};},
    GetPlayerName(id){return id;}, GetPlayerTeamName(){return 'CT';},
    GetPlayerStatsJSO(){return {status:0};},
    SetPlayerMuted(...args){mutes.push(args);},
    IsDemoOrHltv(){return true;}
  }
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);
const hud = sandbox.SwiftDemoVoice;
const array = value => Array.from(value);
hud.Refresh(false);
assert.deepStrictEqual(array(hud.FilterSpeakingSlotsForSelection([1,2,3,12])), [1,2]);
// Halftime/slot reuse: filter stable SteamIDs, not whichever side/slot they started on.
players = [{xuid:'111',slot:8},{xuid:'999',slot:1}];
hud.Refresh(false);
assert.deepStrictEqual(array(hud.FilterSpeakingSlotsForSelection([1,8])), [8]);
assert.deepStrictEqual(array(hud.SpeakingSlotsForTick(110,{holdTicks:30,pulsesBySlot:{8:[100],1:[101]}})),[1,8]);
assert.deepStrictEqual(array(hud.SpeakingSlotsForTick(140,{holdTicks:30,pulsesBySlot:{8:[100]}})),[]);
hud.SelectAll(); hud.SelectNone(); hud.SelectTeam('CT');
assert.deepStrictEqual(commands, []);
assert.deepStrictEqual(mutes, []);
hud.ToggleMenuVisible();
assert.strictEqual(dock.visible, false);
assert.strictEqual(toggle.visible, false);
console.log('Actual Swift runtime: team filtering, overlap, expiry, controls and audio isolation passed');
