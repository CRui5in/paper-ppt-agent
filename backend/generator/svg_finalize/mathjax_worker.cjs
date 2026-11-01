// One local TeX engine per generation worker. Input/output are JSON lines.
const path = require('node:path');
const {createRequire} = require('node:module');
const readline = require('node:readline');
const load = createRequire(path.resolve(__dirname, '../../../frontend/package.json'));
const {mathjax} = load('@mathjax/src/js/mathjax.js');
const {TeX} = load('@mathjax/src/js/input/tex.js');
const {SVG} = load('@mathjax/src/js/output/svg.js');
const {liteAdaptor} = load('@mathjax/src/js/adaptors/liteAdaptor.js');
const {RegisterHTMLHandler} = load('@mathjax/src/js/handlers/html.js');
const {MathJaxNewcmFont} = load('@mathjax/mathjax-newcm-font/js/svg.js');
// Font ranges are installed locally; no network loader is used.
mathjax.asyncLoad = (file) => Promise.resolve(load(file));
const packages = {
  base: 'base/BaseConfiguration', ams: 'ams/AmsConfiguration',
  newcommand: 'newcommand/NewcommandConfiguration', boldsymbol: 'boldsymbol/BoldsymbolConfiguration',
  mathtools: 'mathtools/MathtoolsConfiguration', cancel: 'cancel/CancelConfiguration',
  cases: 'cases/CasesConfiguration', color: 'color/ColorConfiguration',
  unicode: 'unicode/UnicodeConfiguration', textmacros: 'textmacros/TextMacrosConfiguration',
  textcomp: 'textcomp/TextcompConfiguration', upgreek: 'upgreek/UpgreekConfiguration',
  braket: 'braket/BraketConfiguration',
};
for (const file of Object.values(packages)) load(`@mathjax/src/js/input/tex/${file}.js`);
const adaptor = liteAdaptor({fontSize: 16});
RegisterHTMLHandler(adaptor);
const output = new SVG({fontData: MathJaxNewcmFont, fontCache: 'none'});
const lines = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
function tree(node) {
  if (adaptor.kind(node) === '#text') return adaptor.value(node);
  return {tag: adaptor.kind(node), attributes: Object.fromEntries(adaptor.allAttributes(node).map(({name, value}) => [name, value])),
    children: adaptor.childNodes(node).map(tree)};
}
(async () => {
  for await (const line of lines) {
    try {
      const request = JSON.parse(line);
      const input = new TeX({packages: Object.keys(packages), formatError(_jax, error) {throw error;}});
      const document = mathjax.document('', {
        InputJax: input, OutputJax: output,
        compileError(_doc, _math, error) {throw error;},
        typesetError(_doc, _math, error) {throw error;},
      });
      const ex = output.font.params.x_height * 1000;
      const node = await document.convertPromise(request.expression, {display: true, em: 1000, ex, containerWidth: 80000});
      const svg = adaptor.firstChild(node);
      const width = adaptor.getAttribute(svg, 'width');
      const minimum = adaptor.getStyle(svg, 'min-width');
      const w = parseFloat(width === '100%' ? minimum : width) * ex;
      const h = parseFloat(adaptor.getAttribute(svg, 'height')) * ex;
      adaptor.setAttribute(svg, 'width', String(w));
      adaptor.setAttribute(svg, 'height', String(h));
      if (!adaptor.getAttribute(svg, 'viewBox')) adaptor.setAttribute(svg, 'viewBox', `0 0 ${w} ${h}`);
      const stylesheet = output.styleSheet(document);
      process.stdout.write(JSON.stringify({tree: tree(svg), css: stylesheet ? adaptor.textContent(stylesheet) : ''}) + '\n');
    } catch (error) {
      process.stdout.write(JSON.stringify({error: error.message}) + '\n');
    }
  }
})().catch((error) => {console.error(error.message); process.exitCode = 1;});
