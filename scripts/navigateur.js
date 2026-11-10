// Navigateur headless partagé par capture.js, scenario.js et les scripts d'interaction des agents.
//
//   const { ouvrir } = require('<plugin>/scripts/navigateur.js');
//   const n = await ouvrir({ ecriture: true });      // n.page est une page Playwright
//   await n.page.goto('http://127.0.0.1:4110/');
//   n.bilan(); await n.fermer();
//
// Ce que le module garantit, pour qu'un script écrit à la main n'ait pas à le refaire :
// - seules les requêtes vers cette machine passent (localhost, 127.0.0.1, ::1) ; tout autre hôte est
//   bloqué et listé : aucune fuite vers la production, les CDN ou des services tiers ;
// - les méthodes autres que GET sont bloquées, sauf `ecriture: true` (instance de dev sur données temporaires) ;
// - chaque action expire au bout de 15 s et la navigation au bout de 30 s, au lieu d'attendre sans fin ;
// - le script entier est arrêté au bout de `dureeMax` secondes (100 par défaut, sous le délai de 120 s de
//   l'outil Bash) avec un message clair : un script bloqué ne coûte plus trois minutes.
// Options (ou variables d'environnement) : largeur (CAPTURE_LARGEUR, 1440), hauteur (CAPTURE_HAUTEUR, 900),
// mobile (CAPTURE_MOBILE=1 : 390 x 844, tactile), cookie "nom=valeur" (CAPTURE_COOKIE) avec `origine`,
// ecriture (CAPTURE_ECRITURE=1), dureeMax (CAPTURE_DUREE_MAX), delaiAction en ms (15000).
const fs = require('fs');
const os = require('os');
const path = require('path');

const LOCAUX = new Set(['localhost', '127.0.0.1', '[::1]', '::1']);
const ETAT = process.env.VERIFIED_WORKFLOW_ETAT
  || path.join(os.homedir(), '.claude', 'plugins', 'data', 'verified-workflow-verified-workflow');
const OUTILS = path.join(ETAT, 'outils');

function estLocale(url) {
  try {
    const adresse = new URL(url);
    return LOCAUX.has(adresse.hostname) || adresse.protocol === 'data:' || adresse.protocol === 'blob:' || adresse.protocol === 'about:';
  } catch {
    return false;
  }
}

// Chromium de Playwright déjà présent dans ~/.cache/ms-playwright : pas de téléchargement
function trouverChromium() {
  const cache = path.join(os.homedir(), '.cache', 'ms-playwright');
  const candidats = [];
  for (const dossier of fs.existsSync(cache) ? fs.readdirSync(cache) : []) {
    const revision = Number(dossier.split('-').pop()) || 0;
    if (dossier.startsWith('chromium_headless_shell-')) {
      candidats.push([revision, 1, path.join(cache, dossier, 'chrome-headless-shell-linux64', 'chrome-headless-shell')]);
    } else if (dossier.startsWith('chromium-')) {
      candidats.push([revision, 0, path.join(cache, dossier, 'chrome-linux64', 'chrome')]);
    }
  }
  candidats.sort((a, b) => b[0] - a[0] || b[1] - a[1]);
  const trouve = candidats.find((c) => fs.existsSync(c[2]));
  return trouve ? trouve[2] : null;
}

// playwright-core : celui du dossier courant s'il y en a un, sinon l'installation commune du plugin
function chargerPlaywright() {
  for (const base of [process.cwd(), OUTILS]) {
    try {
      return require(require.resolve('playwright-core', { paths: [base] }));
    } catch {
      // essai suivant
    }
  }
  console.log('ERREUR : playwright-core introuvable. Installation, une fois pour toutes :');
  console.log('  npm install --silent --prefix ' + OUTILS + ' playwright-core');
  process.exit(1);
}

async function ouvrir(options = {}) {
  const env = process.env;
  const mobile = options.mobile ?? env.CAPTURE_MOBILE === '1';
  const largeur = Number(options.largeur ?? env.CAPTURE_LARGEUR) || (mobile ? 390 : 1440);
  const hauteur = Number(options.hauteur ?? env.CAPTURE_HAUTEUR) || (mobile ? 844 : 900);
  const ecriture = options.ecriture ?? env.CAPTURE_ECRITURE === '1';
  const cookie = options.cookie ?? env.CAPTURE_COOKIE;
  const dureeMax = Number(options.dureeMax ?? env.CAPTURE_DUREE_MAX) || 100;
  const delaiAction = Number(options.delaiAction) || 15000;

  const executable = trouverChromium();
  if (!executable) {
    console.log('ERREUR : aucun Chromium dans ~/.cache/ms-playwright (npx playwright install chromium-headless-shell).');
    process.exit(1);
  }
  const { chromium } = chargerPlaywright();
  const bloquees = [];
  const erreurs = [];
  const bilan = () => {
    console.log('erreurs console : ' + erreurs.length);
    for (const e of erreurs.slice(0, 30)) console.log('  ' + e);
    console.log('requêtes bloquées : ' + bloquees.length);
    for (const r of [...new Set(bloquees)].slice(0, 30)) console.log('  ' + r);
  };
  // Arrêt automatique : le minuteur ne retient pas le processus s'il finit avant
  const minuteur = setTimeout(() => {
    console.log('DÉLAI DÉPASSÉ : le script tourne depuis ' + dureeMax + ' s, il est arrêté. Ce qui a été fait jusque-là reste valable.');
    bilan();
    process.exit(3);
  }, dureeMax * 1000);
  minuteur.unref();

  const navigateur = await chromium.launch({ executablePath: executable });
  const contexte = await navigateur.newContext({
    viewport: { width: largeur, height: hauteur },
    ...(mobile ? { isMobile: true, hasTouch: true, deviceScaleFactor: 2 } : {}),
  });
  contexte.setDefaultTimeout(delaiAction);
  contexte.setDefaultNavigationTimeout(delaiAction * 2);
  if (cookie && options.origine) {
    const [nom, ...reste] = String(cookie).split('=');
    await contexte.addCookies([{ name: nom, value: reste.join('='), url: new URL(options.origine).origin }]);
  }
  await contexte.route('**/*', (route) => {
    const requete = route.request();
    if (estLocale(requete.url()) && (requete.method() === 'GET' || ecriture)) return route.continue();
    bloquees.push(requete.method() + ' ' + requete.url());
    return route.abort();
  });
  const page = await contexte.newPage();
  page.on('console', (m) => { if (m.type() === 'error') erreurs.push(m.text()); });
  page.on('pageerror', (e) => erreurs.push('pageerror : ' + e.message));

  return {
    navigateur, contexte, page, bloquees, erreurs, bilan,
    async fermer() {
      clearTimeout(minuteur);
      await navigateur.close();
    },
  };
}

module.exports = { ouvrir, estLocale, LOCAUX, OUTILS };
