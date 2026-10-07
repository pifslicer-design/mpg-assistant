-- Lot 1 — Best Team : tables PostgreSQL + RLS
-- À exécuter dans le dashboard Supabase (projet rnqvttsqeonemxrapoiz)
-- ou via : supabase db push

-- 1. Tous les joueurs L1
CREATE TABLE IF NOT EXISTS l1_players (
  id              TEXT PRIMARY KEY,
  first_name      TEXT,
  last_name       TEXT,
  club_id         TEXT,
  ultra_position  INTEGER,
  position        TEXT,           -- "G"/"D"/"M"/"A"
  quotation       INTEGER,
  quotation_trend TEXT,
  average_rating  REAL,
  total_goals     INTEGER,
  total_assists   INTEGER,
  total_played    INTEGER,
  total_started   INTEGER,
  total_yellow    INTEGER,
  total_red       INTEGER,
  updated_at      TIMESTAMPTZ DEFAULT now()
);

-- 2. Historique notes L1 match par match
CREATE TABLE IF NOT EXISTS l1_player_ratings (
  player_id       TEXT REFERENCES l1_players(id),
  season          INTEGER,
  game_week       INTEGER,
  rating          REAL,
  goals           INTEGER DEFAULT 0,
  assists         INTEGER DEFAULT 0,
  minutes_played  INTEGER,
  is_home         BOOLEAN,
  opponent_club   TEXT,
  match_date      DATE,
  PRIMARY KEY (player_id, season, game_week)
);
CREATE INDEX IF NOT EXISTS idx_ratings_lookup ON l1_player_ratings(player_id, season DESC, game_week DESC);

-- 3. Rosters MPG (qui possède qui)
CREATE TABLE IF NOT EXISTS mpg_rosters (
  player_id     TEXT REFERENCES l1_players(id),
  person_id     TEXT NOT NULL,
  team_name     TEXT,
  division_id   TEXT,
  price         REAL,
  PRIMARY KEY (player_id, division_id)
);

-- 4. Prochain match L1 par club
CREATE TABLE IF NOT EXISTS l1_next_matches (
  club_id       TEXT PRIMARY KEY,
  opponent_id   TEXT,
  is_home       BOOLEAN,
  match_date    TIMESTAMPTZ,
  game_week     INTEGER,
  season        INTEGER,
  updated_at    TIMESTAMPTZ DEFAULT now()
);

-- 5. Calendrier MPG (prochain match par joueur)
CREATE TABLE IF NOT EXISTS mpg_schedule (
  person_id       TEXT PRIMARY KEY,
  team_name       TEXT,
  opponent_id     TEXT,
  opponent_name   TEXT,
  game_week       INTEGER,
  season_finished BOOLEAN DEFAULT false,
  updated_at      TIMESTAMPTZ DEFAULT now()
);

-- RLS : lecture publique, écriture service_role uniquement
ALTER TABLE l1_players ENABLE ROW LEVEL SECURITY;
ALTER TABLE l1_player_ratings ENABLE ROW LEVEL SECURITY;
ALTER TABLE mpg_rosters ENABLE ROW LEVEL SECURITY;
ALTER TABLE l1_next_matches ENABLE ROW LEVEL SECURITY;
ALTER TABLE mpg_schedule ENABLE ROW LEVEL SECURITY;

CREATE POLICY "read" ON l1_players FOR SELECT USING (true);
CREATE POLICY "read" ON l1_player_ratings FOR SELECT USING (true);
CREATE POLICY "read" ON mpg_rosters FOR SELECT USING (true);
CREATE POLICY "read" ON l1_next_matches FOR SELECT USING (true);
CREATE POLICY "read" ON mpg_schedule FOR SELECT USING (true);
