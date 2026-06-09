/*!
 * Dark Mode Switch v1.0.1 (https://github.com/coliff/dark-mode-switch)
 * Copyright 2021 C.Oliff
 * Licensed under MIT (https://github.com/coliff/dark-mode-switch/blob/main/LICENSE)
 */

/*
*
* Controls the dark mode/light mode switcher and saves a cookie to remember the
* user preference.
*/

(() => {
    var darkSwitch = document.getElementById("darkSwitch");

    window.addEventListener("load", function () {
      if (darkSwitch) {
          initTheme();
          darkSwitch.addEventListener("change", function () {
            resetTheme();
          });
      }
    });
})();

/**
 * Summary: function that adds or removes the attribute 'color-theme' depending if
 * the switch is 'on' or 'off'.
 *
 * Description: initTheme is a function that uses localStorage from JavaScript DOM,
 * to store the value of the HTML switch. If the switch was already switched to
 * 'on' it will set an HTML attribute to the body named: 'color-theme' to a 'dark'
 * value. If it is the first time opening the page, or if the switch was off the
 * 'color-theme' attribute will not be set.
 * @return {void}
 */
function initTheme() {

  /******
   * Changes to this function likely need to be copied to
   * themes/geekboot/layouts/partials/stylesheet-cached.html
   ******/

  // Is the system set to dark mode?
  var darkPrefered = window.matchMedia('(prefers-color-scheme: dark)').matches

  // The DOM element for the color mode toggle
  var darkSwitch = document.getElementById("darkSwitch");

  // Do we have a dark mode cookie saved?
  var darkThemeSelected =
    localStorage.getItem("darkSwitch") !== null &&
    localStorage.getItem("darkSwitch") === "dark";

  // Do we have a light mode cookie saved?
  var lightThemeSelected =
    localStorage.getItem("darkSwitch") !== null &&
    localStorage.getItem("darkSwitch") === "light";

  // if a light mode cookie is saved ignore the system dark mode
  if (lightThemeSelected) {
    darkPrefered = false;
  }

  // Set the document color theme based on the user's system or cookie  preference
  if (darkThemeSelected || darkPrefered) {
    document.documentElement.setAttribute("color-theme", "dark")
    darkSwitch.checked = true;
  }
  else {
    document.documentElement.setAttribute("color-theme", "light")
    darkSwitch.checked = false;
  }
}

/**
 * Summary: resetTheme checks if the switch is 'on' or 'off' and if it is toggled
 * on it will set the HTML attribute 'color-theme' to dark so the dark-theme CSS is
 * applied.
 * @return {void}
 */
function resetTheme() {

  var darkSwitch = document.getElementById("darkSwitch");

  if (darkSwitch.checked) {
    document.documentElement.setAttribute("color-theme", "dark");
    localStorage.setItem("darkSwitch", "dark");
  } else {
    document.documentElement.setAttribute("color-theme", "light");
    localStorage.setItem("darkSwitch", "light");
  }
}