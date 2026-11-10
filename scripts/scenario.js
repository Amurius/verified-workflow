// Joue un parcours dans une page locale, décrit en JSON, sans écrire de script Playwright.
// Blocage des hôtes non locaux et des écritures, délais et arrêt automatique : voir navigateur.js.
//
// Usage : node scenario.js <scenario.json> [dossier de sortie]
//
// {
//   "base": "http://127.0.0.1:4110",          URL de l'instance de dev
//   "ecriture": true,                          laisse passer POST/PUT/DELETE (instance de dev seulement)
//   "mobile": false, "cookie": "session=...", "duree_max": 100,
//   "etapes": [
//     { "aller": "/" },
//     { "attendre": "h1" },                            sélecteur Playwright (CSS, text=..., role=...)
//     { "remplir": ["input.quantite", "3"] },
//     { "cliquer": "text=Valider" },
//     { "touche": "Enter" },
//     { "attendre_texte": "13,50 €" },                 n'importe où dans la page
//     { "verifier_texte": [".total", "13,50 €"] },    l'élément contient ce texte
//     { "verifier_absent": ".erreur" },
//     { "lire": ".total" },                            affiche le texte de l'élément
//     { "evaluer": "document.title" },                 affiche la valeur d'une expression de la page
//     { "capture": "apres.png" },                      dans le dossier de sortie
//     { "html": "page.html" },
//     { "pause": 300 }                                 millisecondes, 2000 au plus
//   ]
// }
// Chaque étape affiche OK ou KO. Le parcours s'arrête au premier KO (une capture `echec-<n>.png` est prise),
// sauf "continuer": true. Code de retour : 0 tout est OK, 1 un KO, 3 délai dépassé.
const fs = require('fs');
const path = require('path');
const { ouvrir, estLocale } = require('./navigateur.js');

const [cheminScenario, dossierSortie = '.'] = process.argv.slice(2);
if (!cheminScenario) {
  console.log('USAGE : node scenario.js <scenario.json> [dossier de sortie]');
  process.exit(2);
}
let scenario;
try {
  scenario = JSON.parse(fs.readFileSync(cheminScenario, 'utf8'));
} catch (erreur) {
  console.log('ERREUR : scénario illisible : ' + erreur.message);
  process.exit(2);
}
const etapes = Array.isArray(scenario.etapes) ? scenario.etapes : [];
if (!scenario.base || !estLocale(scenario.base)) {
  console.log('REFUS : "base" doit être une URL locale (http://127.0.0.1:<port>).');
  process.exit(2);
}
fs.mkdirSync(dossierSortie, { recursive: true });
const sortie = (nom) => path.resolve(dossierSortie, path.basename(String(nom)));

async function jouer(page, etape) {
  const [action, valeur] = Object.entries(etape)[0] || [];
  switch (action) {
    case 'aller': {
      const cible = new URL(String(valeur), scenario.base).href;
      if (!estLocale(cible)) throw new Error('REFUS : ' + cible + " n'est pas une URL locale");
      const reponse = await page.goto(cible, { waitUntil: 'load' });
      return 'statut ' + (reponse ? reponse.status() : '?') + ', ' + page.url();
    }
    case 'attendre':
      await page.locator(String(valeur)).first().waitFor();
      return 'présent';
    case 'cliquer':
      await page.locator(String(valeur)).first().click();
      return '';
    case 'remplir':
      await page.locator(String(valeur[0])).first().fill(String(valeur[1]));
      return '';
    case 'touche':
      await page.keyboard.press(String(valeur));
      return '';
    case 'attendre_texte':
      await page.getByText(String(valeur)).first().waitFor();
      return 'vu';
    case 'verifier_texte': {
      const element = page.locator(String(valeur[0])).first();
      await element.waitFor();
      const limite = Date.now() + 5000;
      let texte = '';
      do {
        texte = (await element.innerText()).trim();
        if (texte.includes(String(valeur[1]))) return '« ' + texte.slice(0, 120) + ' »';
        await page.waitForTimeout(100);
      } while (Date.now() < limite);
      throw new Error('attendu « ' + valeur[1] + ' », lu « ' + texte.slice(0, 200) + ' »');
    }
    case 'verifier_absent': {
      const nombre = await page.locator(String(valeur)).count();
      if (nombre) throw new Error(nombre + ' élément(s) présent(s)');
      return 'absent';
    }
    case 'lire':
      return '« ' + (await page.locator(String(valeur)).first().innerText()).trim().slice(0, 500) + ' »';
    case 'evaluer':
      return JSON.stringify(await page.evaluate(String(valeur)));
    case 'capture':
      await page.screenshot({ path: sortie(valeur), fullPage: true });
      return sortie(valeur);
    case 'html':
      fs.writeFileSync(sortie(valeur), await page.content(), 'utf8');
      return sortie(valeur);
    case 'pause':
      await page.waitForTimeout(Math.min(Number(valeur) || 0, 2000));
      return '';
    default:
      throw new Error('action inconnue « ' + action + ' »');
  }
}

(async () => {
  const n = await ouvrir({
    origine: scenario.base, ecriture: scenario.ecriture, mobile: scenario.mobile, cookie: scenario.cookie,
    largeur: scenario.largeur, hauteur: scenario.hauteur, dureeMax: scenario.duree_max,
  });
  let echecs = 0;
  try {
    for (let i = 0; i < etapes.length; i += 1) {
      const etiquette = 'étape ' + (i + 1) + ' ' + JSON.stringify(etapes[i]);
      try {
        const detail = await jouer(n.page, etapes[i]);
        console.log('OK  ' + etiquette + (detail ? ' -> ' + detail : ''));
      } catch (erreur) {
        echecs += 1;
        console.log('KO  ' + etiquette + ' -> ' + erreur.message.split('\n')[0]);
        await n.page.screenshot({ path: sortie('echec-' + (i + 1) + '.png'), fullPage: true }).catch(() => {});
        console.log('    capture de l\'état : ' + sortie('echec-' + (i + 1) + '.png'));
        if (!scenario.continuer) {
          console.log('    parcours arrêté : ' + (etapes.length - i - 1) + ' étape(s) non jouée(s)');
          break;
        }
      }
    }
  } finally {
    n.bilan();
    await n.fermer();
  }
  console.log(echecs ? 'RÉSULTAT : ' + echecs + ' étape(s) KO' : 'RÉSULTAT : toutes les étapes sont OK');
  process.exit(echecs ? 1 : 0);
})();
