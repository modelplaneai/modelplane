// The collection of things to load on all pages

// Color mode switcher
import ColorMode from './colorMode';

// Link pre-fetcher
// https://instant.page/
import 'instant.page';

// Bootstrap JS libraries in use.
// IF a new Bootstrap feature requires JS, add it here.
import './bootstrap/src/base-component';
import './bootstrap/src/button';
import './bootstrap/src/collapse';
import './bootstrap/src/dropdown';
import './bootstrap/src/popover';
import './bootstrap/src/scrollspy';
import './bootstrap/src/tab';
import './bootstrap/src/offcanvas';

// If a link is to an anchor inside a tab, open the tab and go to the anchor
import './tabDeepAnchor.js';

// Customize the clipboard to support the `copy-lines` function
import './customClipboard.js';

// Hover to highlight function
import './hoverHighlight.js';

// "Join Slack" notification bubble
import './slackNotify.js';