import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const ANTHROPIC_API_KEY = Deno.env.get("ANTHROPIC_API_KEY");

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

// ---------- Types ----------
interface Player {
  player_id: string;
  first_name: string | null;
  last_name: string | null;
  position: string;
  club_id: string;
  ultra_position: number;
  average_rating: number | null;
  quotation: number | null;
  quotation_trend: string | null;
  total_goals: number;
  total_assists: number;
  total_played: number;
}

interface Rating {
  season: number;
  game_week: number;
  rating: number;
  goals: number;
  assists: number;
  is_home: boolean;
}

interface ScoredPlayer {
  player_id: string;
  name: string;
  position: string;
  club_id: string;
  score: number;
  average_rating: number | null;
  quotation: number | null;
  quotation_trend: string | null;
  data_flag: string | null;
  matches_in_window: number;
  next_l1: { opponent_id: string | null; is_home: boolean | null; game_week: number | null } | null;
}

// ---------- Formations ----------
const FORMATIONS: Record<string, Record<string, number>> = {
  "3-4-3": { G: 1, D: 3, M: 4, A: 3 },
  "3-5-2": { G: 1, D: 3, M: 5, A: 2 },
  "4-3-3": { G: 1, D: 4, M: 3, A: 3 },
  "4-4-2": { G: 1, D: 4, M: 4, A: 2 },
  "4-5-1": { G: 1, D: 4, M: 5, A: 1 },
  "5-3-2": { G: 1, D: 5, M: 3, A: 2 },
  "5-4-1": { G: 1, D: 5, M: 4, A: 1 },
};

// ---------- Scoring ----------
function computeScore(ratings: Rating[], nextMatch: { is_home: boolean | null } | null): { score: number; matches: number; goalRate: number; assistRate: number } {
  const n = ratings.length;
  if (n === 0) return { score: 0, matches: 0, goalRate: 0, assistRate: 0 };

  const decay = 0.85;
  let weightedSum = 0;
  let weightSum = 0;
  let weightedGoals = 0;
  let weightedAssists = 0;

  // ratings are ordered most recent first
  for (let i = 0; i < n; i++) {
    const w = Math.pow(decay, i);
    weightedSum += ratings[i].rating * w;
    weightSum += w;
    weightedGoals += ratings[i].goals * w;
    weightedAssists += ratings[i].assists * w;
  }

  const weightedAvg = weightedSum / weightSum;
  const goalRate = weightedGoals / weightSum;
  const assistRate = weightedAssists / weightSum;
  const homeBonus = nextMatch?.is_home ? 0.15 : 0;

  const score = weightedAvg + goalRate * 0.3 + assistRate * 0.15 + homeBonus;
  return { score, matches: n, goalRate, assistRate };
}

function getDataFlag(matches: number): string | null {
  if (matches < 3) return "\u26a0\ufe0f donn\u00e9es limit\u00e9es";
  if (matches <= 5) return "donn\u00e9es limit\u00e9es";
  return null;
}

// ---------- Best formation ----------
function findBestFormation(players: ScoredPlayer[], forcedFormation?: string): { formation: string; starters: ScoredPlayer[]; subs: ScoredPlayer[] } {
  const byPos: Record<string, ScoredPlayer[]> = { G: [], D: [], M: [], A: [] };
  for (const p of players) {
    if (byPos[p.position]) byPos[p.position].push(p);
  }
  // Sort each position by score desc
  for (const pos of Object.keys(byPos)) {
    byPos[pos].sort((a, b) => b.score - a.score);
  }

  let bestFormation = "";
  let bestScore = -Infinity;
  let bestStarters: ScoredPlayer[] = [];

  const formationsToTest = forcedFormation && FORMATIONS[forcedFormation]
    ? { [forcedFormation]: FORMATIONS[forcedFormation] }
    : FORMATIONS;

  for (const [fname, slots] of Object.entries(formationsToTest)) {
    // Check we have enough players per position
    let valid = true;
    let starters: ScoredPlayer[] = [];
    let totalScore = 0;

    for (const [pos, count] of Object.entries(slots)) {
      if (byPos[pos].length < count) { valid = false; break; }
      const picked = byPos[pos].slice(0, count);
      starters = starters.concat(picked);
      totalScore += picked.reduce((s, p) => s + p.score, 0);
    }

    // Bonus défense MPG : 5D → +1.0/def, 4D → +0.5/def
    if (valid) {
      const defCount = slots["D"] ?? 0;
      if (defCount >= 5) totalScore += defCount * 1.0;
      else if (defCount === 4) totalScore += defCount * 0.5;
    }

    if (valid && totalScore > bestScore) {
      bestScore = totalScore;
      bestFormation = fname;
      bestStarters = starters;
    }
  }

  const starterIds = new Set(bestStarters.map(p => p.player_id));
  const subs = players
    .filter(p => !starterIds.has(p.player_id))
    .sort((a, b) => b.score - a.score);

  return { formation: bestFormation, starters: bestStarters, subs };
}

// ---------- Captain ----------
function pickCaptain(starters: ScoredPlayer[]): { player: ScoredPlayer; reason: string } {
  // Filter eligible: average_rating > 6
  let candidates = starters.filter(p => (p.average_rating ?? 0) > 6);
  // Fallback: if no one has avg > 6, pick from all starters
  if (candidates.length === 0) candidates = [...starters];

  candidates.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score;
    // Prefer ATT/MO (A then M) for captain upside
    const posOrder: Record<string, number> = { A: 0, M: 1, D: 2, G: 3 };
    return (posOrder[a.position] ?? 9) - (posOrder[b.position] ?? 9);
  });

  const captain = candidates[0];
  const reason = `Meilleur score (${captain.score.toFixed(2)}) \u2014 ${captain.position === "A" ? "attaquant, gros upside bonus" : captain.position === "M" ? "milieu offensif, bon upside" : "solide et r\u00e9gulier"}`;
  return { player: captain, reason };
}

// ---------- Claude Haiku commentary ----------
async function generateCommentary(
  formation: string,
  starters: { name: string; position: string; score: number; data_flag: string | null }[],
  captain: { name: string; position: string },
  opponent: string,
  gameWeek: number
): Promise<string> {
  if (!ANTHROPIC_API_KEY) return "(Commentaire IA indisponible \u2014 cl\u00e9 API manquante)";

  const posLabels: Record<string, string> = { G: "Gardien", D: "Défenseur", M: "Milieu", A: "Attaquant" };
  const startersList = starters.map(s => `${s.name} (${posLabels[s.position] || s.position}, score ${s.score.toFixed(2)}${s.data_flag ? `, ${s.data_flag}` : ""})`).join(", ");

  // Parse formation to get explicit counts per position
  const parts = formation.split("-").map(Number);
  const formationDetail = `${formation} (1 gardien, ${parts[0]} défenseurs, ${parts[1]} milieux, ${parts[2]} attaquants)`;

  const fragilePlayers = starters.filter(s => s.data_flag).map(s => s.name);
  const fragileNote = fragilePlayers.length > 0
    ? `\nJoueurs à surveiller (données limitées ou forme incertaine) : ${fragilePlayers.join(", ")}.`
    : "";

  const prompt = `Tu es une IA qui vient de composer cette équipe MPG et qui assume complètement ses choix. Tu commentes la compo dans le groupe WhatsApp de la ligue \u2014 avec humour, vannes légères, en mode pote taquin. Tutoiement, 3-4 phrases fluides, pas de listes, pas de "Yo" systématique.

Règles absolues :
- Tu ASSUMES ta compo, tu ne la remets JAMAIS en cause
- Tu ne suggères PAS de changer des joueurs (roster figé)
- Le chambrage porte sur : l'adversaire ("${opponent} va souffrir", "bon courage à eux"), les joueurs fragiles ou en méforme de ta propre équipe (avec une vanne affectueuse), la situation tactique (ex: si un seul attaquant, il faut qu'il soit chaud), et le capitaine (justifier le choix avec une vanne)

Journée ${gameWeek} contre ${opponent}, formation ${formationDetail}.
Compo : ${startersList}
Capitaine : ${captain.name} (${posLabels[captain.position] || captain.position})${fragileNote}`;

  try {
    const resp = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
      },
      body: JSON.stringify({
        model: "claude-haiku-4-5-20251001",
        max_tokens: 300,
        messages: [{ role: "user", content: prompt }],
      }),
    });
    if (!resp.ok) {
      const err = await resp.text();
      console.error("Anthropic API error:", err);
      return "(Commentaire IA indisponible)";
    }
    const data = await resp.json();
    return data.content?.[0]?.text ?? "(Pas de commentaire)";
  } catch (e) {
    console.error("Anthropic call failed:", e);
    return "(Commentaire IA indisponible)";
  }
}

// ---------- Main handler ----------
Deno.serve(async (req: Request) => {
  // CORS
  if (req.method === "OPTIONS") {
    return new Response(null, {
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
      },
    });
  }

  if (req.method !== "POST") {
    return new Response(JSON.stringify({ error: "Method not allowed" }), { status: 405, headers: { "Content-Type": "application/json" } });
  }

  try {
    const { person_id, unavailable = [], window: windowSize = 10, formation: requestedFormation = "" } = await req.json();

    if (!person_id) {
      return new Response(JSON.stringify({ error: "person_id required" }), { status: 400, headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" } });
    }

    // 1. Load MPG schedule for this person
    const { data: schedule } = await supabase
      .from("mpg_schedule")
      .select("*")
      .eq("person_id", person_id)
      .single();

    if (!schedule) {
      return new Response(JSON.stringify({ error: `No schedule found for ${person_id}` }), { status: 404, headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" } });
    }

    if (schedule.season_finished) {
      return new Response(JSON.stringify({
        person_id,
        team_name: schedule.team_name,
        season_finished: true,
        message: "Saison termin\u00e9e, rendez-vous \u00e0 la prochaine !"
      }), { headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" } });
    }

    // 2. Load roster + player data
    const { data: rosterRows } = await supabase
      .from("mpg_rosters")
      .select("player_id, price")
      .eq("person_id", person_id);

    if (!rosterRows || rosterRows.length === 0) {
      return new Response(JSON.stringify({ error: `No roster found for ${person_id}` }), { status: 404, headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" } });
    }

    const unavailableSet = new Set(unavailable as string[]);
    const availablePlayerIds = rosterRows
      .map(r => r.player_id)
      .filter(id => !unavailableSet.has(id));

    // Load player details
    const { data: playersData } = await supabase
      .from("l1_players")
      .select("id, first_name, last_name, position, club_id, ultra_position, average_rating, quotation, quotation_trend, total_goals, total_assists, total_played")
      .in("id", availablePlayerIds);

    if (!playersData || playersData.length === 0) {
      return new Response(JSON.stringify({ error: "No player data found" }), { status: 404, headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" } });
    }

    // 3. Load ratings (rolling window) for all roster players
    const { data: ratingsData } = await supabase
      .from("l1_player_ratings")
      .select("player_id, season, game_week, rating, goals, assists, is_home")
      .in("player_id", availablePlayerIds)
      .order("season", { ascending: false })
      .order("game_week", { ascending: false })
      .limit(availablePlayerIds.length * windowSize);

    // Group ratings by player, keep only top N per player
    const ratingsByPlayer: Record<string, Rating[]> = {};
    for (const r of (ratingsData ?? [])) {
      if (!ratingsByPlayer[r.player_id]) ratingsByPlayer[r.player_id] = [];
      if (ratingsByPlayer[r.player_id].length < windowSize) {
        ratingsByPlayer[r.player_id].push(r);
      }
    }

    // 4. Load next L1 matches
    const clubIds = [...new Set(playersData.map(p => p.club_id).filter(Boolean))];
    const { data: nextMatches } = await supabase
      .from("l1_next_matches")
      .select("club_id, opponent_id, is_home, game_week")
      .in("club_id", clubIds);

    const nextMatchByClub: Record<string, { opponent_id: string; is_home: boolean; game_week: number }> = {};
    for (const m of (nextMatches ?? [])) {
      nextMatchByClub[m.club_id] = m;
    }

    // 5. Score all players
    const scoredPlayers: ScoredPlayer[] = [];
    for (const p of playersData) {
      const ratings = ratingsByPlayer[p.id] ?? [];
      const nextL1 = nextMatchByClub[p.club_id] ?? null;
      const { score, matches } = computeScore(ratings, nextL1);
      const dataFlag = getDataFlag(matches);

      scoredPlayers.push({
        player_id: p.id,
        name: `${p.first_name ?? ""} ${p.last_name ?? ""}`.trim(),
        position: p.position,
        club_id: p.club_id,
        score: dataFlag === "\u26a0\ufe0f donn\u00e9es limit\u00e9es" ? 0 : score,
        average_rating: p.average_rating,
        quotation: p.quotation,
        quotation_trend: p.quotation_trend,
        data_flag: dataFlag,
        matches_in_window: matches,
        next_l1: nextL1 ? { opponent_id: nextL1.opponent_id, is_home: nextL1.is_home, game_week: nextL1.game_week } : null,
      });
    }

    // 6. Validate requested formation if any
    if (requestedFormation && !FORMATIONS[requestedFormation]) {
      return new Response(JSON.stringify({ error: `Formation inconnue : ${requestedFormation}. Valides : ${Object.keys(FORMATIONS).join(", ")}` }), { status: 400, headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" } });
    }

    // 7. Find best formation
    const { formation, starters, subs } = findBestFormation(scoredPlayers, requestedFormation || undefined);

    if (!formation) {
      const msg = requestedFormation
        ? `Pas assez de joueurs pour la formation ${requestedFormation}`
        : "Not enough players to form a valid team";
      return new Response(JSON.stringify({ error: msg }), { status: 400, headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" } });
    }

    // 7. Pick captain
    const { player: captainPlayer, reason: captainReason } = pickCaptain(starters);

    // Mark captain in starters
    const startersOut = starters.map(s => ({
      name: s.name,
      player_id: s.player_id,
      position: s.position,
      club_id: s.club_id,
      score: Math.round(s.score * 100) / 100,
      captain: s.player_id === captainPlayer.player_id,
      data_flag: s.data_flag,
      quotation: s.quotation,
      quotation_trend: s.quotation_trend,
      matches_in_window: s.matches_in_window,
      next_l1: s.next_l1,
    }));

    const subsOut = subs.map(s => ({
      name: s.name,
      player_id: s.player_id,
      position: s.position,
      club_id: s.club_id,
      score: Math.round(s.score * 100) / 100,
      data_flag: s.data_flag,
      quotation: s.quotation,
      matches_in_window: s.matches_in_window,
    }));

    // 8. Generate AI commentary
    const commentary = await generateCommentary(
      formation,
      starters.map(s => ({ name: s.name, position: s.position, score: s.score, data_flag: s.data_flag })),
      { name: captainPlayer.name, position: captainPlayer.position },
      schedule.opponent_name ?? schedule.opponent_id,
      schedule.game_week
    );

    // 9. Build response
    const response = {
      person_id,
      team_name: schedule.team_name,
      formation,
      next_match: {
        opponent: schedule.opponent_name ?? schedule.opponent_id,
        game_week: schedule.game_week,
      },
      starters: startersOut,
      substitutes: subsOut,
      captain: {
        name: captainPlayer.name,
        player_id: captainPlayer.player_id,
        position: captainPlayer.position,
        score: Math.round(captainPlayer.score * 100) / 100,
        reason: captainReason,
      },
      commentary,
      meta: {
        window: windowSize,
        generated_at: new Date().toISOString(),
        data_freshness: (playersData as any)[0]?.updated_at ?? null,
        total_roster: rosterRows.length,
        available: availablePlayerIds.length,
        unavailable_count: unavailable.length,
      },
    };

    return new Response(JSON.stringify(response), {
      headers: {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
      },
    });
  } catch (e) {
    console.error("Error:", e);
    return new Response(JSON.stringify({ error: (e as Error).message }), {
      status: 500,
      headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
    });
  }
});
