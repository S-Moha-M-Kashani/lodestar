import { THEME_KEY } from '../core/keys.js';
import { $ } from './dom.js';

// The theme picker. Five themes, and the choice is remembered.

const THEMES = ['light', 'white', 'sepia', 'dark', 'star'];
const themeSelect = $('#theme-select');

// The phone's status bar, in the theme the board is wearing. Installed on a
// home screen there is no address bar to draw a boundary, so an unchanged
// <meta name="theme-color"> leaves a white band over the star theme's night
// sky. Read back off the root rather than listed here: the five themes each
// define --paper in styles.css, and a second copy of those hexes in JavaScript
// is a sixth theme nobody is maintaining.
function paintStatusBar() {
  const meta = document.querySelector('meta[name="theme-color"]');
  if (!meta) return;
  const paper = getComputedStyle(document.documentElement)
    .getPropertyValue('--paper').trim();
  if (paper) meta.content = paper;
}

function applyTheme(theme) {
  if (!THEMES.includes(theme)) theme = 'light';
  document.documentElement.dataset.theme = theme;
  themeSelect.value = theme;
  paintStatusBar();
}

themeSelect.addEventListener('change', () => {
  applyTheme(themeSelect.value);
  try { localStorage.setItem(THEME_KEY, themeSelect.value); } catch (_) { /* private mode */ }
});

let savedTheme = null;
try { savedTheme = localStorage.getItem(THEME_KEY); } catch (_) { /* private mode */ }
applyTheme(savedTheme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
