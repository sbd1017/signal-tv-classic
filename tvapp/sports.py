"""Sports names and events in the guide: aliases, sport detection and match recognition.

Used by the guide index (epgsearch.py) for the Live search and the Sports view. Everything here
is a small maintained dataset plus conservative rules; nothing fuzzy, nothing fetched.

Aliases (DATA below, one entity per line; easy to extend):
- strong names ("Manchester United | Man Utd | Man United") are equivalent everywhere: searching
  one finds the others in titles and descriptions;
- weak names ("Spurs", "Inter", "Villa", "PL") are trusted only as a whole side of a recognised
  match ("Spurs v Arsenal"), or as the label of one ("PL 26/27: Chelsea v B'mouth"); typing a weak
  name that belongs to exactly one entity also searches for that entity;
- "not after" / "not before" words stop an alias where a qualifier makes it something else
  ("Champions League" after AFC, EHF, Global...; "Premier League" after Indian, or before Darts);
- "United", "City", "Real" and the like are never names on their own.

Matches: "A v B", "A vs B", "A x B", "A @ B" (and "A - B" only when both sides are known teams or a
sport label says so: "Nogomet: Como - Roma", "Boks: X - Y"), with a competition/label prefix,
round/date tails and country codes removed. A sport comes from, strongest first: a sport word in
the title (many languages: "Košarka" makes "Barcelona - Valencia" basketball), a competition,
known clubs, the channel. Highlights, magazines, classics, build-up and documentaries are
recognised and kept out of the live-match lists. Unknown teams count only on a sports channel or
with sports evidence: "Kramer vs. Kramer" on a film channel is not a match.
"""
from __future__ import annotations

import re
import unicodedata

# ------------------------------------------------------------------ sports
FOOTBALL, BASKETBALL, F1, MMA, BOXING, NFL, TENNIS, CRICKET, RUGBY, HOCKEY, HANDBALL, VOLLEYBALL, OTHER = range(1, 14)
SPORT_KEYS = {FOOTBALL: "football", BASKETBALL: "basketball", F1: "f1", MMA: "mma", BOXING: "boxing", NFL: "nfl",
              TENNIS: "tennis", CRICKET: "cricket", RUGBY: "rugby", HOCKEY: "hockey", HANDBALL: "handball",
              VOLLEYBALL: "volleyball", OTHER: "other"}
SPORT_LABELS = {FOOTBALL: "Football", BASKETBALL: "Basketball", F1: "Formula 1", MMA: "UFC / MMA", BOXING: "Boxing",
                NFL: "NFL", TENNIS: "Tennis", CRICKET: "Cricket", RUGBY: "Rugby", HOCKEY: "Ice hockey",
                HANDBALL: "Handball", VOLLEYBALL: "Volleyball", OTHER: "Other sport"}
BY_KEY = {v: k for k, v in SPORT_KEYS.items()}

# kinds of sports programme
MATCH, EVENT, SHOW = 1, 2, 3          # a match / a race, fight night or session / highlights, magazine, build-up...

_APOS = re.compile(r"['’`´]")
_NONWORD = re.compile(r"[\W_]+")
_MARKS = re.compile(r"[̀-ͯ]")      # combining accents, as the page's fold() (api.js)


def norm(text: str) -> str:
    """Case, accents, punctuation and spacing don't matter: "Women's Champions-League: Barça"
    -> "womens champions league barca"."""
    text = (text or "").lower()
    if not text.isascii():
        text = _MARKS.sub("", unicodedata.normalize("NFKD", text))
    return _NONWORD.sub(" ", _APOS.sub("", text)).strip()


# ------------------------------------------------------------------ the dataset
# @kind sport   starts a section. Line: id = Name | alias | alias ; weak: a | b ; not after: w w ;
# not before: w w ; priority: N (competitions: how prominent, 0-100) ; was: old-id old-id
# The first name is the one shown. The id ("team:arsenal", "comp:premier-league") is what favourites
# store: never change one. To rename, change the names; to merge two entries, keep one id and list
# the other under "was:" so favourites saved with it still work.
DATA = r"""
@club football
team:arsenal = Arsenal
team:aston-villa = Aston Villa ; weak: Villa
team:afc-bournemouth = AFC Bournemouth | Bournemouth ; weak: B'mouth
team:brentford = Brentford
team:brighton-hove-albion = Brighton & Hove Albion | Brighton and Hove Albion | Brighton ; weak: B&HA
team:burnley = Burnley
team:chelsea = Chelsea
team:crystal-palace = Crystal Palace ; weak: Palace | C Palace
team:everton = Everton
team:fulham = Fulham
team:leeds-united = Leeds United | Leeds ; weak: Leeds Utd
team:liverpool = Liverpool
team:manchester-city = Manchester City | Man City ; weak: Man. City
team:manchester-united = Manchester United | Man United | Man Utd | Manchester Utd ; weak: ManU | Man U
team:newcastle-united = Newcastle United | Newcastle ; weak: Newcastle Utd
team:nottingham-forest = Nottingham Forest | Nott'm Forest | Nottm Forest ; weak: Forest | N Forest
team:sunderland = Sunderland
team:tottenham-hotspur = Tottenham Hotspur | Tottenham | Spurs ; not after: san antonio
team:west-ham-united = West Ham United | West Ham ; weak: West Ham Utd
team:wolverhampton-wanderers = Wolverhampton Wanderers | Wolves
team:leicester-city = Leicester City | Leicester
team:southampton = Southampton
team:ipswich-town = Ipswich Town | Ipswich
team:sheffield-united = Sheffield United | Sheffield Utd | Sheff Utd
team:sheffield-wednesday = Sheffield Wednesday | Sheff Wed
team:west-bromwich-albion = West Bromwich Albion | West Brom ; weak: West Bromwich
team:middlesbrough = Middlesbrough
team:norwich-city = Norwich City | Norwich
team:coventry-city = Coventry City | Coventry
team:watford = Watford
team:stoke-city = Stoke City | Stoke
team:hull-city = Hull City | Hull
team:derby-county = Derby County ; weak: Derby
team:queens-park-rangers = Queens Park Rangers | QPR
team:birmingham-city = Birmingham City ; weak: Birmingham
team:wrexham = Wrexham
team:celtic = Celtic
team:rangers = Rangers ; weak: Glasgow Rangers
team:aberdeen = Aberdeen
team:heart-of-midlothian = Heart of Midlothian | Hearts
team:real-madrid = Real Madrid
team:barcelona = Barcelona | FC Barcelona | Barca ; weak: Barcelone | Barcelonne | Barca FC
team:atletico-madrid = Atlético Madrid | Atletico de Madrid | Atleti ; weak: Atletico | A Madrid | Atl Madrid | Atletico Madryt
team:athletic-club = Athletic Club | Athletic Bilbao ; weak: Bilbao | Athletic
team:real-sociedad = Real Sociedad
team:real-betis = Real Betis | Betis
team:sevilla = Sevilla ; weak: Sevilla FC
team:valencia-cf = Valencia CF ; weak: Valencia
team:villarreal = Villarreal ; weak: V'Real | Villareal
team:celta-vigo = Celta Vigo | Celta de Vigo | RC Celta ; weak: Celta | C Vigo
team:girona = Girona
team:osasuna = Osasuna
team:getafe = Getafe
team:rayo-vallecano = Rayo Vallecano ; weak: Rayo
team:mallorca = Mallorca | RCD Mallorca
team:espanyol = Espanyol
team:juventus = Juventus | Juve
team:inter-milan = Inter Milan | FC Internazionale | Inter Milano ; weak: Inter | Internazionale
team:ac-milan = AC Milan ; weak: Milan
team:napoli = Napoli | SSC Napoli
team:as-roma = AS Roma ; weak: Roma
team:lazio = Lazio | SS Lazio
team:atalanta = Atalanta
team:fiorentina = Fiorentina
team:bologna = Bologna
team:torino = Torino
team:como = Como ; weak: Como 1907
team:venezia = Venezia ; weak: Veneza
team:udinese = Udinese
team:cagliari = Cagliari
team:genoa = Genoa
team:sassuolo = Sassuolo
team:parma = Parma
team:lecce = Lecce
team:monza = Monza
team:bayern-munich = Bayern Munich | Bayern München | Bayern Munchen | Bayern Muenchen | FC Bayern | FC Bayern Munchen ; weak: Bayern
team:borussia-dortmund = Borussia Dortmund | BVB ; weak: Dortmund
team:bayer-leverkusen = Bayer Leverkusen | Leverkusen | Bayer 04 Leverkusen
team:rb-leipzig = RB Leipzig ; weak: Leipzig
team:eintracht-frankfurt = Eintracht Frankfurt ; weak: Frankfurt
team:vfb-stuttgart = VfB Stuttgart ; weak: Stuttgart
team:borussia-monchengladbach = Borussia Mönchengladbach | Monchengladbach | Gladbach
team:vfl-wolfsburg = VfL Wolfsburg ; weak: Wolfsburg
team:sc-freiburg = SC Freiburg ; weak: Freiburg
team:werder-bremen = Werder Bremen ; weak: Bremen
team:union-berlin = Union Berlin | 1. FC Union Berlin
team:hamburger-sv = Hamburger SV | Hamburg SV ; weak: Hamburg | HSV
team:paris-saint-germain = Paris Saint-Germain | Paris St Germain | Paris St-Germain | Paris SG | PSG
team:marseille = Marseille | Olympique de Marseille ; weak: OM
team:lyon = Lyon | Olympique Lyonnais ; weak: OL
team:as-monaco = AS Monaco ; weak: Monaco
team:lille = Lille | LOSC Lille ; weak: LOSC
team:ogc-nice = OGC Nice ; weak: Nice
team:rc-lens = RC Lens ; weak: Lens
team:stade-rennais = Stade Rennais | Rennes
team:benfica = Benfica | SL Benfica ; weak: SLB
team:fc-porto = FC Porto ; weak: Porto
team:sporting-cp = Sporting CP | Sporting Lisbon | Sporting Clube de Portugal | Sporting Lissabon | Sporting Lisabona ; weak: Sporting | SCP
team:sc-braga = SC Braga ; weak: Braga
team:ajax = Ajax | AFC Ajax
team:psv-eindhoven = PSV Eindhoven | PSV
team:feyenoord = Feyenoord
team:az-alkmaar = AZ Alkmaar ; weak: AZ
team:galatasaray = Galatasaray
team:fenerbahce = Fenerbahçe
team:besiktas = Beşiktaş | Besiktas JK | Besiktas AS
team:trabzonspor = Trabzonspor
team:club-brugge = Club Brugge
team:anderlecht = Anderlecht
team:red-bull-salzburg = Red Bull Salzburg | RB Salzburg ; weak: Salzburg
team:shakhtar-donetsk = Shakhtar Donetsk ; weak: Shakhtar
team:dinamo-zagreb = Dinamo Zagreb ; weak: GNK Dinamo
team:red-star-belgrade = Red Star Belgrade | Crvena Zvezda
team:olympiacos = Olympiacos
team:panathinaikos = Panathinaikos
team:al-hilal = Al Hilal
team:al-nassr = Al Nassr
team:al-ittihad = Al Ittihad
team:al-ahli-saudi = Al Ahli Saudi ; weak: Al Ahli
team:inter-miami = Inter Miami
team:la-galaxy = LA Galaxy
team:flamengo = Flamengo
team:palmeiras = Palmeiras
team:corinthians = Corinthians
team:boca-juniors = Boca Juniors
team:river-plate = River Plate
team:club-america = Club America ; weak: America

@national any
team:england = England
team:scotland = Scotland
team:wales = Wales
team:northern-ireland = Northern Ireland
team:republic-of-ireland = Republic of Ireland | Ireland
team:france = France
team:germany = Germany | Deutschland
team:spain = Spain | Espana
team:italy = Italy | Italia
team:portugal = Portugal
team:netherlands = Netherlands | Nederland ; weak: Holland
team:belgium = Belgium | Belgique | Belgie
team:brazil = Brazil | Brasil
team:argentina = Argentina
team:united-states = United States | USA | USMNT
team:mexico = Mexico
team:croatia = Croatia | Hrvatska
team:denmark = Denmark | Danmark
team:sweden = Sweden | Sverige
team:norway = Norway | Norge
team:poland = Poland | Polska
team:turkey = Turkey | Turkiye
team:switzerland = Switzerland | Schweiz | Suisse
team:austria = Austria | Osterreich
team:morocco = Morocco | Maroc
team:japan = Japan
team:south-korea = South Korea | Korea Republic
team:australia = Australia ; weak: AUS
team:uruguay = Uruguay
team:colombia = Colombia
team:saudi-arabia = Saudi Arabia
team:egypt = Egypt
team:nigeria = Nigeria
team:senegal = Senegal
team:india = India ; weak: IND
team:pakistan = Pakistan ; weak: PAK
team:south-africa = South Africa ; weak: RSA | SA
team:new-zealand = New Zealand ; weak: NZ
team:west-indies = West Indies ; weak: WI
team:sri-lanka = Sri Lanka ; weak: SL
team:bangladesh = Bangladesh ; weak: BAN
team:afghanistan = Afghanistan ; weak: AFG
team:zimbabwe = Zimbabwe ; weak: ZIM
team:finland = Finland | Suomi
team:iceland = Iceland | Island
team:greece = Greece | Hellas
team:serbia = Serbia | Srbija
team:czechia = Czechia | Czech Republic | Cesko
team:hungary = Hungary | Magyarorszag
team:romania = Romania
team:ukraine = Ukraine
team:slovakia = Slovakia
team:slovenia = Slovenia | Slovenija
team:albania = Albania
team:bosnia-and-herzegovina = Bosnia and Herzegovina | Bosnia-Herzegovina
team:north-macedonia = North Macedonia
team:montenegro = Montenegro
team:israel = Israel
team:canada = Canada
team:chile = Chile
team:peru = Peru
team:ecuador = Ecuador
team:paraguay = Paraguay
team:iran = Iran
team:qatar = Qatar
team:ghana = Ghana
team:cameroon = Cameroon
team:ivory-coast = Ivory Coast | Cote d'Ivoire
team:algeria = Algeria
team:tunisia = Tunisia

@competition football
comp:premier-league = Premier League | English Premier League | EPL | Barclays Premier League | Premier liga ; weak: PL ; priority: 100 ; not after: indian bangladesh caribbean pakistan lanka lankan egyptian egypt nigeria nigerian ghana ghanaian kenyan kenya russian ukrainian israeli welsh kong armenian belarusian kazakhstan tanzanian maltese jamaica jamaican lebanese singapore malaysian canadian hockey netball volleyball basketball darts snooker kabaddi ; not before: darts netball hockey kabaddi
comp:uefa-champions-league = UEFA Champions League | Champions League | UCL | Champions-League | Liga de Campeones | Ligue des champions | Liga dos Campeoes ; priority: 100 ; not after: afc caf ehf cev concacaf ofc global asian african basketball hockey volleyball womens t20 velux
comp:uefa-womens-champions-league = UEFA Women's Champions League | Women's Champions League | UWCL ; priority: 45
comp:uefa-europa-league = UEFA Europa League | Europa League | UEL | Liga Europa | Ligue Europa ; priority: 85
comp:uefa-conference-league = UEFA Conference League | Conference League | Europa Conference League | UECL ; priority: 70
comp:laliga = LaLiga | La Liga | LaLiga EA Sports | Spanish LaLiga | Primera Division ; priority: 90 ; not after: liga
comp:laliga-hypermotion = LaLiga Hypermotion | LaLiga 2 | Segunda Division ; priority: 45
comp:serie-a = Serie A | Serie A TIM | Serie A Enilive ; priority: 85 ; not after: brasileirao campeonato brasileiro basket volley lba
comp:bundesliga = Bundesliga ; priority: 85 ; not after: handball basketball basket volleyball hockey eishockey 2 osterreichische austrian admiral frauen
comp:2-bundesliga = 2. Bundesliga | 2 Bundesliga | Zweite Bundesliga ; priority: 45
comp:ligue-1 = Ligue 1 | French Ligue 1 | Ligue 1 McDonald's ; priority: 75
comp:ligue-2 = Ligue 2 ; priority: 40
comp:eredivisie = Eredivisie ; priority: 55
comp:primeira-liga = Primeira Liga | Liga Portugal | Liga Portugal Betclic ; priority: 55 ; not before: 2
comp:liga-portugal-2 = Liga Portugal 2 ; priority: 25
comp:scottish-premiership = Scottish Premiership | SPFL | Scottish Premier League ; priority: 55
comp:super-lig = Super Lig | Trendyol Super Lig ; priority: 50 ; not after: volleyball basketball
comp:saudi-pro-league = Saudi Pro League | Roshn Saudi League ; priority: 50
comp:major-league-soccer = Major League Soccer | MLS ; priority: 50
comp:liga-mx = Liga MX ; priority: 45
comp:brasileirao = Brasileirao | Brasileirao Serie A | Campeonato Brasileiro ; priority: 55
comp:efl-championship = EFL Championship | Sky Bet Championship | SkyBet Championship ; weak: Championship ; priority: 60 ; not after: racing truck touring car rally rallycross superbike superbikes supersport motocross enduro endurance karting speedway drift gt motorcycle motogp darts snooker pool rugby netball hockey golf swimming athletics gymnastics cycling sailing boxing world european national ; not before: series
comp:efl-league-one = EFL League One | Sky Bet League One ; weak: League One ; priority: 35
comp:efl-league-two = EFL League Two | Sky Bet League Two ; weak: League Two ; priority: 30
comp:fa-cup = FA Cup | Emirates FA Cup ; priority: 70 ; not after: womens scottish
comp:efl-cup = EFL Cup | Carabao Cup ; weak: League Cup ; priority: 65
comp:copa-del-rey = Copa del Rey ; priority: 60
comp:coppa-italia = Coppa Italia ; priority: 55
comp:dfb-pokal = DFB-Pokal | DFB Pokal ; priority: 55
comp:coupe-de-france = Coupe de France ; priority: 45
comp:fifa-world-cup = FIFA World Cup ; priority: 100
comp:fifa-club-world-cup = FIFA Club World Cup | Club World Cup ; priority: 80
comp:world-cup-qualifiers = World Cup Qualifiers | World Cup Qualifying | WC Qualifiers ; priority: 70 ; not after: rugby cricket t20 icc
comp:uefa-nations-league = UEFA Nations League ; weak: Nations League ; priority: 65
comp:uefa-euro = UEFA Euro | European Championship Qualifiers | Euro Qualifiers ; priority: 85
comp:copa-libertadores = Copa Libertadores | Libertadores ; priority: 60
comp:copa-sudamericana = Copa Sudamericana ; priority: 40
comp:afc-champions-league = AFC Champions League | AFC Champions League Elite | ACL Elite ; priority: 40
comp:uefa-youth-league = UEFA Youth League ; priority: 10
comp:caf-champions-league = CAF Champions League ; priority: 35
comp:womens-super-league = Women's Super League | Barclays WSL ; weak: WSL ; priority: 40
comp:jupiler-pro-league = Jupiler Pro League | Belgian Pro League ; priority: 30
comp:eliteserien = Eliteserien ; priority: 25
comp:allsvenskan = Allsvenskan ; priority: 25
comp:ekstraklasa = Ekstraklasa ; priority: 25
comp:qatar-stars-league = Qatar Stars League | Stars League | QSL ; priority: 25
comp:uae-pro-league = UAE Pro League ; priority: 25
comp:a-league = A-League | A League Men ; priority: 30

@competition basketball
comp:nba = NBA ; priority: 80 ; not before: 2k
comp:wnba = WNBA ; priority: 30
comp:euroleague = EuroLeague | Euroliga | Turkish Airlines EuroLeague ; priority: 50
comp:eurocup = EuroCup ; priority: 30 ; not after: rugby
comp:liga-endesa = Liga Endesa | Liga ACB | ACB ; priority: 40
comp:aba-league = ABA League | ABA liga ; priority: 25

@competition nfl
comp:nfl = NFL ; priority: 80

@competition cricket
comp:indian-premier-league = Indian Premier League | IPL ; priority: 60
comp:t20-world-cup = T20 World Cup | ICC Men's T20 World Cup ; priority: 70
comp:the-ashes = The Ashes | Ashes Test ; priority: 80
comp:the-hundred = The Hundred ; priority: 30

@competition rugby
comp:six-nations = Six Nations | Guinness Six Nations | 6 Nations ; priority: 80
comp:rugby-world-cup = Rugby World Cup ; priority: 90
comp:premiership-rugby = Premiership Rugby | Gallagher Premiership ; priority: 50
comp:united-rugby-championship = United Rugby Championship | URC ; priority: 45
comp:top-14 = Top 14 ; priority: 45
comp:super-rugby = Super Rugby ; priority: 40
comp:the-rugby-championship = The Rugby Championship ; priority: 60
comp:nrl = NRL ; priority: 40
comp:investec-champions-cup = Investec Champions Cup | European Rugby Champions Cup ; priority: 50

@competition tennis
comp:wimbledon = Wimbledon ; priority: 90
comp:roland-garros = Roland Garros | French Open ; priority: 80
comp:australian-open = Australian Open ; priority: 80
comp:atp-finals = ATP Finals ; priority: 60
comp:atp-tour = ATP Tour | ATP ; priority: 35
comp:wta-tour = WTA Tour | WTA ; priority: 30
comp:davis-cup = Davis Cup ; priority: 40

@competition f1
comp:formula-1 = Formula 1 | F1 | Formula One | Formel 1 | Formula Uno | Formule 1 | FIA Formula One World Championship ; priority: 90

@competition mma
comp:ufc = UFC ; priority: 80
comp:pfl = PFL ; priority: 30
comp:bellator = Bellator ; priority: 30
comp:one-championship = ONE Championship ; priority: 25
"""

# Sport words in titles and descriptions (any language seen in this guide). Matched as whole
# words / phrases on norm() text; longer phrases first ("american football" before "football").
TERMS = {
    NFL: "american football | americky fotbal | americki fudbal | am nogomet | americki nogomet | ameriski nogomet | "
         "futbol americano | football americain | "
         "american football | αμερικανικο ποδοσφαιρο | американский футбол | nfl",
    FOOTBALL: "football | soccer | futbol | futebol | calcio | fussball | fußball | voetbal | fotboll | fodbold | "
              "fotball | jalkapallo | fotbal | nogomet | fudbal | labdarugas | pilka nozna | piłka nozna | ποδοσφαιρο | "
              "футбол | futbal | kopaonik",
    BASKETBALL: "basketball | basket | baloncesto | pallacanestro | kosarka | koszykowka | kosarlabda | koripallo | "
                "basquete | basquetebol | μπασκετ | баскетбол | basketbol | 3x3",
    HANDBALL: "handball | handbold | handboll | rukomet | balonmano | pallamano | pilka reczna | piłka reczna | kezilabda | hazena | "
              "hentbol | χαντμπολ | гандбол | starligue | handball bundesliga",
    VOLLEYBALL: "volleyball | volley | odbojka | siatkowka | voleibol | pallavolo | roplabda | volejbal | lentopallo | "
                "voleybol | βολει | волейбол",
    HOCKEY: "ice hockey | hockey | nhl | khl | hokej | jaakiekko | ishockey | eishockey | хоккей",
    TENNIS: "tennis | tenis | tenisz",
    BOXING: "boxing | boks | boxe | boxeo | pugilato | бокс",
    MMA: "mma | mixed martial arts | fight night",
    F1: "grand prix | formula 1 | f1",
    CRICKET: "cricket | odi | t20 | t20i | test match | icc | cri | kriket",
    RUGBY: "rugby | rugby union | rugby league | ragbi",
    OTHER: "golf | darts | snooker | cycling | ciclismo | motogp | moto2 | moto3 | nascar | indycar | superbike | "
           "athletics | swimming | equestrian | sailing | sail gp | horse racing | motor racing | bowling | poker | wrestling | "
           "wwe | aew | padel | badminton | table tennis | esports | kabaddi | netball | volleyball nations",
}
# Not a live sports programme (kept for search, left out of the Sports view's live lists).
SHOW_WORDS = ("highlights | highlight | hl | hlts | hls | hoogtepunten | magazine | magazin | review | preview | greatest | classic | classics | "
              "rewind | best of | goals | stories | story | documentary | docu | doku | dokumentation | build up | "
              "buildup | countdown | studio | show | weekly | talk | news | analysis | roundup | round up | "
              "all or nothing | match of the day | motd | goal rush | moments | momenti | momentos | hojdpunkter | "
              "hoydepunkter | hojdepunkter | sammendrag | resumen | resumo | sintesi | zusammenfassung | samenvatting | "
              "melhores momentos | pre match | prematch | post match | postmatch | reaction | press conference | "
              "training | legends | icons | rivalry | rivals | inside | behind the | academy | debate | podcast | "
              "quiz | vlog | this week | tribute | history | archive | anthology | special | specials | nejlepsi momenty | "
              "greatest games | the story | movie | film | official film")
REPLAY_WORDS = ("replay | rerun | re run | re air | reair | delayed | deferred | repeat | wiederholung | repeticion | "
                "reprise | rediffusion | replica | en diferido | diferido | powtorka | ponovitev | encore")
LIVE_WORDS = "live | en vivo | en directo | ao vivo | en direct | direct | diretta | na zivo | uzivo | v zivo | live now"
WOMEN_WORDS = {"women", "womens", "w", "ladies", "feminino", "femenino", "femminile", "frauen", "damer", "kvinner",
               "kvinnor", "feminine", "zene", "kobiet", "noi", "ž"}
# Words that make an unknown side look like a football club ("Esteghlal FC", "Racing Club").
CLUB_WORDS = {"fc", "cf", "afc", "sc", "cd", "ud", "sd", "ac", "as", "ss", "ssc", "sv", "vfb", "vfl", "fk", "nk", "sk",
              "bk", "if", "ik", "united", "city", "athletic", "rovers", "wanderers", "albion", "town", "county",
              "real", "atletico", "sporting", "deportivo", "dynamo", "dinamo", "spartak", "cska", "olympique",
              "borussia", "calcio", "club", "hotspur"}
GENERIC = {"fc", "cf", "afc", "sc", "cd", "ud", "sd", "ssc", "sk", "fk", "nk", "club", "calcio", "the"}
# Youth and reserve sides are other matches than the first team's.
YOUTH_WORDS = {"u17", "u18", "u19", "u20", "u21", "u23", "sub", "youth", "juniors", "junior", "reserves", "primavera",
               "juvenil", "academy", "mlodziezowa", "młodziezowa", "juniorska", "omladinska", "mladinska", "jugend", "giovanile",
               "primavera", "espoirs", "juniores", "u19s", "under", "nextgen"}
_DATE = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{2}|\d{4})\b")
# Tails after the second team: rounds, legs, dates ("3rd T20", "Week 6", "MD 2", "26/27")
_TAIL = re.compile(r"\s+(?:\d+(?:st|nd|rd|th)\b|\d+[/.]\d+|\d{4}\b|'\d\d\b|week\b|wk\b|md\s*\d|mw\s*\d|round\b|leg\b|"
                   r"game\s*\d|g\d+\b|semi|final|quarter|test\b|odi\b|t20|matchday|jornada|giornata|spieltag|journee|"
                   r"etapa\b|runda\b|kolo\b|hafta\b|fecha\b|speeldag\b|omgang\b|kierros\b|forduló)",
                   re.I)
_VS = re.compile(r"\s(?:v|vs|vs\.|v\.|x|@)\s", re.I)
_DASH = re.compile(r"\s[-–—/]\s")
_HYPHEN = re.compile(r"(?<=[^\W\d_])-(?=[^\W\d_])")
_PAREN = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_LABEL = re.compile(r"\s*:\s*(?!\d\d\b)")
# A side that is a part of an event, never a team: "Race 1 - Jarama", "Heat 2 - Lohéac", "Stage 5"
_NOT_A_SIDE = re.compile(r"^(race|heat|stage|leg|round|session|practice|qualifying|sprint|semi final|final|run|moto)( \d+)?$")
_LEADLIVE = re.compile(r"^\s*(?:live|en vivo|en directo|ao vivo|en direct|direct|diretta)\b\s*[:!|-]?\s*", re.I)


def _phrases(text: str) -> list[str]:
    return [p for p in (norm(x) for x in text.split("|")) if p]


class Entity:
    __slots__ = ("id", "key", "kind", "sport", "name", "names", "weak", "not_after", "not_before", "priority", "was")

    def __init__(self, eid: int, key: str, kind: str, sport: int, name: str):
        self.id, self.key, self.kind, self.sport, self.name = eid, key, kind, sport, name
        self.was: list[str] = []
        self.names: list[str] = []
        self.weak: list[str] = []
        self.not_after: set[str] = set()
        self.not_before: set[str] = set()
        self.priority = 0


def _load(data: str) -> list[Entity]:
    out: list[Entity] = []
    kind, sport = "", 0
    for raw in data.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            kind, sp = line[1:].split()
            sport = 0 if sp == "any" else BY_KEY[sp]
            continue
        key, sep, line = line.partition(" = ")
        if not sep or not re.fullmatch(r"(team|comp):[a-z0-9-]+", key):
            raise ValueError(f"sports DATA line without an id: {raw!r}")
        parts = [p.strip() for p in line.split(";")]
        names = [n.strip() for n in parts[0].split("|") if n.strip()]
        e = Entity(len(out), key, kind, sport, names[0])
        e.names = [n for n in dict.fromkeys(norm(n) for n in names) if n]
        for opt in parts[1:]:
            name, _, val = opt.partition(":")
            name = name.strip()
            if name == "weak":
                e.weak = [n for n in dict.fromkeys(norm(n) for n in val.split("|")) if n]
            elif name == "not after":
                e.not_after = set(norm(val).split())
            elif name == "not before":
                e.not_before = set(norm(val).split())
            elif name == "priority":
                e.priority = int(val)
            elif name == "was":
                e.was = val.split()
        out.append(e)
    return out


ENTITIES = _load(DATA)
BY_ID_KEY: dict[str, Entity] = {}          # stable id (and old ids) -> entity: what favourites resolve through
for _e in ENTITIES:
    for _k in (_e.key, *_e.was):
        if _k in BY_ID_KEY:
            raise ValueError(f"sports DATA: id {_k} used twice")
        BY_ID_KEY[_k] = _e
STRONG: dict[str, list[int]] = {}          # phrase -> entities (a phrase can only be strong for one)
WEAK: dict[str, list[int]] = {}
for _e in ENTITIES:
    for _p in _e.names:
        STRONG.setdefault(_p, []).append(_e.id)
    for _p in _e.weak:
        WEAK.setdefault(_p, []).append(_e.id)
MAXLEN = max(len(p.split()) for p in list(STRONG) + list(WEAK))
_TERMS: dict[str, int] = {}
for _s, _txt in TERMS.items():
    for _p in _phrases(_txt):
        _TERMS.setdefault(_p, _s)
_SHOW = set(_phrases(SHOW_WORDS))
_REPLAY = set(_phrases(REPLAY_WORDS))
_LIVE = set(_phrases(LIVE_WORDS))
_TERMLEN = max(len(p.split()) for p in list(_TERMS) + list(_SHOW) + list(_REPLAY))


def _ok_context(e: Entity, words: list[str], i: int, j: int) -> bool:
    before = words[i - 1] if i > 0 else ""
    after = words[j] if j < len(words) else ""
    return before not in e.not_after and after not in e.not_before


def scan(words: list[str], weak: bool = False) -> list[tuple[int, int, int]]:
    """Entities named in a word list: [(start, end, entity id)], longest first, not overlapping.
    weak=True also accepts weak names (callers use it only for a whole match side or label)."""
    out = []
    i, n = 0, len(words)
    while i < n:
        hit = None
        for ln in range(min(MAXLEN, n - i), 0, -1):
            p = " ".join(words[i:i + ln])
            ids = STRONG.get(p) or (WEAK.get(p) if weak else None)
            if ids and len(ids) == 1 and _ok_context(ENTITIES[ids[0]], words, i, i + ln):
                hit = (i, i + ln, ids[0])
                break
        if hit:
            out.append(hit)
            i = hit[1]
        else:
            i += 1
    return out


def _find(words: list[str], table) -> list[tuple[int, int, object]]:
    out = []
    n = len(words)
    for i in range(n):
        for ln in range(min(_TERMLEN, n - i), 0, -1):
            p = " ".join(words[i:i + ln])
            if p in table:
                out.append((i, i + ln, table[p] if isinstance(table, dict) else p))
                break
    return out


def sport_word(words: list[str]) -> int:
    """The sport a sport word names: the first one, except that any NFL word wins over "football"
    ("Monday Night Football ... NFL")."""
    hits = [h[2] for h in _find(words, _TERMS)]
    if not hits:
        return 0
    return NFL if NFL in hits and hits[0] == FOOTBALL else hits[0]


def has_any(words: list[str], table: set) -> bool:
    return bool(_find(words, table))


def resolve_side(side: str) -> tuple[int, str, bool]:
    """A match side -> (entity id or -1, key text, women). Weak names count here: a whole side."""
    words = norm(side).split()
    women = bool(words) and words[-1] in WOMEN_WORDS
    while words and words[-1] in WOMEN_WORDS:
        words.pop()
    text = " ".join(words)
    for cand in (text, " ".join(w for w in words if w not in GENERIC)):
        ids = STRONG.get(cand) or WEAK.get(cand)
        if ids and len(ids) == 1 and ENTITIES[ids[0]].kind in ("club", "national"):
            return ids[0], ENTITIES[ids[0]].key, women
    key = " ".join(w for w in words if w not in GENERIC)
    return -1, key, women


def split_match(title: str) -> tuple[str, str, str, bool] | None:
    """'LaLiga 2026/27: Rayo Vallecano vs Athletic Bilbao' -> (label, left, right, dashed).
    label: whatever came before the teams (competition, sport word); dashed: "A - B" form."""
    t = _PAREN.sub(" ", title)
    t = _LEADLIVE.sub("", t)
    segs = _LABEL.split(t)
    for k in range(len(segs) - 1, -1, -1):
        seg = segs[k]
        m = _VS.search(f" {seg} ")
        dashed = False
        if not m:
            m = _DASH.search(seg)
            dashed = True
            if not m:
                # "Arsenal-Lille": a bare hyphen, only between two known teams
                parts = _HYPHEN.split(seg.split(",")[0])
                if len(parts) == 2 and all(resolve_side(_TAIL.split(x, 1)[0])[0] >= 0 for x in parts):
                    left, right = (_TAIL.split(x, 1)[0] for x in parts)
                    return " ".join(segs[:k]), left.strip(), right.strip(), True
                continue
            left, right = seg[:m.start()], seg[m.end():]
        else:
            left, right = f" {seg} "[:m.start()], f" {seg} "[m.end():]
            d = list(_DASH.finditer(left))
            if d:                                   # "Football - Arsenal v Chelsea"
                left = left[d[-1].end():]
        right = _DASH.split(right, 1)[0].split(",")[0].split("|")[0]
        right = _TAIL.split(right, 1)[0]
        left = _TAIL.split(left, 1)[0] if dashed else left
        label = " ".join(segs[:k])
        if left.strip() and right.strip():
            return label, left.strip(" -–"), right.strip(" -–"), dashed
        return None
    return None


class Info:
    """What a programme is, as far as sport goes."""
    __slots__ = ("sport", "kind", "key", "comp", "teams", "women", "youth", "replay", "live", "explicit", "names",
                 "dated", "from_desc", "comp_desc")

    def __init__(self):
        self.sport = 0
        self.kind = 0
        self.key = ""
        self.comp = -1
        self.teams = (-1, -1)
        self.women = False
        self.youth = False
        self.replay = False
        self.from_desc = False                    # the teams were found in the description, not the title
        self.comp_desc = False                    # the competition too
        self.dated = 0                            # a date in the title (YYYYMMDD): a replay unless it is today
        self.live = False
        self.explicit = False                     # the sport was named (a sport word or competition)
        self.names = ("", "")                     # sides as written, for display


_MAYBE = re.compile(r"\s(?:v|vs|vs\.|x|@)\s|\s[-–—/]\s|:|[^\W\d_]-[^\W\d_]", re.I)


def worth_classifying(title: str, channel_sport: int) -> bool:
    """Cheap pre-check: off sports channels, only titles shaped like a match or with a label."""
    return bool(channel_sport) or bool(_MAYBE.search(title))


def classify(title: str, desc: str, channel_sport: int = 0) -> Info | None:
    """None if the programme isn't recognisably sport. channel_sport: 0 not a sports channel,
    OTHER a sports channel, or the sport its name says (FOOTBALL for "LaLiga TV")."""
    tw = norm(title).split()
    if not tw:
        return None
    info = Info()
    info.replay = has_any(tw, _REPLAY) or "(r)" in title.lower() or "(rep)" in title.lower()
    if re.search(r"\((?:w|women|womens|ž|f|fem)\)", title, re.I):
        info.women = True
    m = _DATE.search(title)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y
        if 1 <= d <= 31 and 1 <= mo <= 12:
            info.dated = y * 10000 + mo * 100 + d
    info.live = bool(_LEADLIVE.match(title)) or has_any(tw[:2], _LIVE)
    show = has_any(tw, _SHOW)
    term = sport_word(tw)
    ents = scan(tw)
    comps = [ENTITIES[e] for _, _, e in ents if ENTITIES[e].kind == "competition"]
    pair = split_match(title)
    teams = (-1, -1)
    clubby = False
    sides: list[str] = []
    if pair:
        label, left, right, dashed = pair
        a, ka, wa = resolve_side(left)
        b, kb, wb = resolve_side(right)
        lw = norm(label).split()
        if not comps and lw:
            comps = [ENTITIES[e] for _, _, e in scan(lw, weak=True) if ENTITIES[e].kind == "competition"]
        term = term or sport_word(lw)
        if term:
            comps = [c for c in comps if c.sport in (0, term)]
        known = (a >= 0) + (b >= 0)
        lw_side = norm(left).split() + norm(right).split()
        not_team = any(sport_word(norm(x).split()) or has_any(norm(x).split(), _SHOW) or _NOT_A_SIDE.match(norm(x)) or
                       any(ENTITIES[e].kind == "competition" for _, _, e in scan(norm(x).split(), weak=True))
                       for x, k in ((left, a), (right, b)) if k < 0)
        clubby = any(w in CLUB_WORDS for w in lw_side)
        long_side = max(len(norm(left).split()), len(norm(right).split())) > 6
        evidence = bool(term or comps or known == 2 or channel_sport)
        if term == F1 or any(c.sport == F1 for c in comps):
            pair = None                              # "Formel 1: Rennen - GP Singapur": no teams in F1
        elif dashed and not (known == 2 or ((term or comps) and channel_sport)):
            pair = None
        elif long_side or ka == kb or not evidence or not_team:
            pair = None
        else:
            teams = (a, b)
            info.women = info.women or wa or wb or any(w in WOMEN_WORDS for w in lw if w not in ("w",))
            info.youth = any(w in YOUTH_WORDS for w in tw) or "b" in (norm(left).split()[-1:] + norm(right).split()[-1:])
            info.names = (left, right)
            sides = sorted([ka, kb])
    dw: list[str] = []
    if not term or not comps:
        dw = norm(desc[:300]).split() if desc else []
    if not term and dw and (pair or comps or channel_sport):
        term = sport_word(dw)
    if not comps and dw and (pair or term or channel_sport):
        comps = [ENTITIES[e] for _, _, e in scan(dw) if ENTITIES[e].kind == "competition"]
        info.comp_desc = bool(comps)
    title_comp = any(ENTITIES[e].kind == "competition" for _, _, e in ents)
    if term:
        comps = [c for c in comps if c.sport in (0, term)]   # "Rugby ... Championship" isn't the EFL's
    comp = comps[0] if comps else None
    # "Premier League" with "...reigning champions Arsenal host Leeds United" in the description
    if not pair and comp and title_comp and not show and dw and len(tw) <= len(comp.name.split()) + 2:
        # clubs only: countries in a description are places ("From Arsenal Stadium - London, England")
        clubs = list(dict.fromkeys(e for _, _, e in scan(dw) if ENTITIES[e].kind == "club"))
        if len(clubs) == 2 and not has_any(dw, _SHOW):
            teams = (clubs[0], clubs[1])
            sides = sorted([ENTITIES[clubs[0]].key, ENTITIES[clubs[1]].key])
            info.names = (ENTITIES[clubs[0]].name, ENTITIES[clubs[1]].name)
            info.from_desc = True
            pair = ("", "", "", False)
    # the sport, strongest evidence first
    sport = term or (comp.sport if comp else 0)
    info.explicit = bool(sport)
    if not sport and pair:
        club_sports = {ENTITIES[t].sport for t in teams if t >= 0 and ENTITIES[t].kind == "club"}
        if len(club_sports) == 1:
            sport = club_sports.pop()
        elif channel_sport and channel_sport != OTHER:
            sport = channel_sport
        elif clubby:
            sport = FOOTBALL
    if not sport and not pair:
        if channel_sport and channel_sport != OTHER and not show and ents:
            sport = channel_sport
        else:
            return None
    info.sport = sport or OTHER
    info.comp = comp.id if comp else -1
    if info.explicit:
        # "NBA: Spurs v Lakers" is not Tottenham: a club is that club only in its own sport. (The
        # event key keeps the names, so the same game listed without a sport word still merges.)
        teams = tuple(t if t < 0 or ENTITIES[t].sport in (0, info.sport) else -1 for t in teams)
    info.teams = teams
    if comp and any(w in YOUTH_WORDS for w in comp.names[0].split()):
        info.youth = True                            # "Leipzig v PSV", description "UEFA Youth League"
    if comp and any(w in WOMEN_WORDS for w in comp.names[0].split()):
        info.women = True                            # "Chelsea v Arsenal" in the Women's Super League
    if pair:
        info.kind = SHOW if show else MATCH
        info.key = ("w|" if info.women else "") + ("y|" if info.youth else "") + "|".join(sides)
    elif sport == F1:
        f1_title = sport_word(tw) == F1 or comp is not None and comp.sport == F1 and title_comp
        session = (_f1_session(tw) or (_f1_session(dw) if dw else "")) if f1_title else ""
        info.kind = EVENT if session and not show else SHOW
        info.key = "f1|" + (session or norm(title))
    elif sport == MMA and not show and re.search(r"\b(ufc \d+|fight night|main card|prelims|pfl \d+)\b", " ".join(tw)):
        info.kind = EVENT
        info.key = norm(title)
    elif sport == TENNIS and not show and comp and (info.live or re.search(
            r"\b(atp|wta|masters|open|finals?|cup|round|ronda|turniej|torneo|tournoi|\d{3,4})\b", " ".join(tw))):
        info.kind = EVENT
        info.key = norm(title)
    else:
        info.kind = SHOW
        info.key = norm(title)
    return info


_F1 = [("sprint qualifying", "sprint-qualifying"), ("sprint shootout", "sprint-qualifying"), ("sprint", "sprint"),
       ("qualifying", "qualifying"), ("qualification", "qualifying"), ("clasificacion", "qualifying"),
       ("qualifiche", "qualifying"), ("quali", "qualifying"), ("kwalificatie", "qualifying"),
       ("practice", "practice"), ("fp1", "practice"), ("fp2", "practice"), ("fp3", "practice"),
       ("libres", "practice"), ("entrenamientos", "practice"), ("libere", "practice"), ("training", "practice"),
       ("race", "race"), ("carrera", "race"), ("corrida", "race"), ("gara", "race"), ("rennen", "race"),
       ("course", "race"), ("wyscig", "race"), ("main race", "race")]


def _f1_session(words: list[str]) -> str:
    text = f" {' '.join(words)} "
    for w, s in _F1:
        if f" {w} " in text:
            return s
    return ""


def display_title(info: Info, title: str) -> str:
    """'Arsenal vs Leeds United' for a recognised match; the guide's title otherwise."""
    if info.kind != MATCH:
        return title
    a, b = info.teams

    def side(e: int, raw: str) -> str:
        # a club's name only in its own sport: basketball's "Valencia" isn't Valencia CF
        if e >= 0 and (ENTITIES[e].sport in (0, info.sport) or not info.explicit):
            return ENTITIES[e].name
        return _tidy(raw) or (ENTITIES[e].name if e >= 0 else "")
    left, right = side(a, info.names[0]), side(b, info.names[1])
    women = " (W)" if info.women else ""
    return f"{left}{women} vs {right}{women}"


def _tidy(side: str) -> str:
    side = re.sub(r"\s+", " ", side).strip(" -–:")
    return side.title() if side.isupper() and len(side) > 4 else side


# ------------------------------------------------------------------ search-side aliases
def query_groups(words: list[str]) -> list[tuple[int, int, int]]:
    """Entities named in a typed query: strong names, and weak names that belong to one entity
    (typing "Spurs" means Tottenham). Only whole typed words count ("man ut" is not yet anything)."""
    out = []
    i, n = 0, len(words)
    while i < n:
        hit = None
        for ln in range(min(MAXLEN, n - i), 0, -1):
            p = " ".join(words[i:i + ln])
            ids = STRONG.get(p) or WEAK.get(p)
            if ids and len(ids) == 1:
                hit = (i, i + ln, ids[0])
                break
        if hit:
            out.append(hit)
            i = hit[1]
        else:
            i += 1
    return out


def phrase_ok(e: Entity, text_words: list[str], start: int, length: int) -> bool:
    return _ok_context(e, text_words, start, start + length)


_FOOTBALL_CH = re.compile(r"\b(laliga|la liga|premier league|serie a|bundesliga|ligue 1|eredivisie|mutv|lfctv|lfc tv|"
                          r"chelsea tv|barca tv|real madrid tv|sky sports football|sky sports premier league|"
                          r"canal\+ foot|foot\+|liga portugal|benfica tv|sporting tv|porto canal|ziggo sport voetbal)\b")
_SPORT_CH = re.compile(r"\b(sports?|sport\d|bein|supersport|dazn|arena sport|sport klub|sportklub|eleven|sportdigital|"
                       r"ziggo|viaplay|v sport|cosmote|nova sport|polsat sport|cytavision|setanta|ssc|alkass|ad sport|"
                       r"on time sport|digi sport|prima sport|tnt sports?|premier sports|sky sport|movistar|m\+|"
                       r"espn|tsn|fox sports|stan sport|optus sport|astro|hub|tvn|canal\+ sport|rmc sport|"
                       r"eurosport|matchroom|ufc|nba|nfl|f1|motors? ?tv)\b")


def channel_sport(name: str, category: str) -> int:
    """What a channel says about its sport: FOOTBALL for "LaLiga TV", "V Fotboll", "MUTV"; another
    sport by its sport word ("NBA TV"); OTHER for a sports channel without one; 0 otherwise."""
    words = norm(name).split()
    s = sport_word(words)
    if s:
        return s
    text = " ".join(words)
    if _FOOTBALL_CH.search(text):
        return FOOTBALL
    if _SPORT_CH.search(text) or _SPORT_CH.search(norm(category)):
        return OTHER
    return 0
