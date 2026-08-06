use parser::first_pass::parser_settings::ParserInputs;
use parser::parse_demo::{Parser, ParsingMode};
use parser::second_pass::parser_settings::create_huffman_lookup_table;
use ahash::AHashMap;
use std::fs::File;
use memmap2::MmapOptions;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let demo = &args[1];
    let steamid: u64 = args[2].parse().unwrap();
    let huf = create_huffman_lookup_table();
    let inputs = ParserInputs {
        wanted_player_props: vec![
            "tick".to_string(), "usercmd_buttonstate_1".to_string(),
            "usercmd_forward_move".to_string(), "usercmd_left_move".to_string(),
            "usercmd_mouse_dx".to_string(), "usercmd_mouse_dy".to_string(),
        ],
        wanted_events: vec![], real_name_to_og_name: AHashMap::default(),
        wanted_other_props: vec![], parse_ents: true, wanted_players: vec![steamid],
        wanted_ticks: vec![], parse_projectiles: false, collect_projectile_records: false,
        parse_grenades: false, only_header: false, list_props: false, only_convars: false,
        huffman_lookup_table: &huf, order_by_steamid: true,
        wanted_prop_states: AHashMap::default(), fallback_bytes: None, cancelled: None,
    };
    let mut ds = Parser::new(inputs, ParsingMode::ForceSingleThreaded);
    let f = File::open(demo).unwrap();
    let mmap = unsafe { MmapOptions::new().map(&f).unwrap() };
    let d = ds.parse_demo(&mmap).unwrap();
    let cols = &d.df_per_player[&steamid];
    use parser::second_pass::variants::VarVec;
    let t  = match cols.get(&100000013).and_then(|c|c.data.as_ref()) { Some(VarVec::I32(v))=>v, _=>&vec![] };
    let b1 = match cols.get(&100000029).and_then(|c|c.data.as_ref()) { Some(VarVec::U64(v))=>v, _=>&vec![] };
    let fw = match cols.get(&100000025).and_then(|c|c.data.as_ref()) { Some(VarVec::F32(v))=>v, _=>&vec![] };
    let lf = match cols.get(&100000033).and_then(|c|c.data.as_ref()) { Some(VarVec::F32(v))=>v, _=>&vec![] };
    let mdx= match cols.get(&100000027).and_then(|c|c.data.as_ref()) { Some(VarVec::I32(v))=>v, _=>&vec![] };
    let mdy= match cols.get(&100000028).and_then(|c|c.data.as_ref()) { Some(VarVec::I32(v))=>v, _=>&vec![] };
    let n = t.len().min(b1.len()).min(fw.len()).min(lf.len()).min(mdx.len()).min(mdy.len());
    for i in 0..n {
        println!("{},{},{},{},{},{}", t[i].unwrap_or(0), b1[i].unwrap_or(0),
            fw[i].unwrap_or(0.0), lf[i].unwrap_or(0.0), mdx[i].unwrap_or(0), mdy[i].unwrap_or(0));
    }
}
