import { THEME_KEY } from '../core/keys.js';
import { $ } from './dom.js';

// The theme picker. Five themes, and the choice is remembered.

const THEMES = ['light', 'white', 'sepia', 'dark', 'star'];
const themeSelect = $('#theme-select');

function applyTheme(theme) {
  if (!THEMES.includes(theme)) theme = 'light';
  document.documentElement.dataset.theme = theme;
  themeSelect.value = theme;
}

themeSelect.addEventListener('change', () => {
  applyTheme(themeSelect.value);
  try { localStorage.setItem(THEME_KEY, themeSelect.value); } catch (_) { /* private mode */ }
});

let savedTheme = null;
try { savedTheme = localStorage.getItem(THEME_KEY); } catch (_) { /* private mode */ }
applyTheme(savedTheme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
