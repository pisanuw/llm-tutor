/** Tailwind build config for the precompiled stylesheet
 *  (tutor/web/static/vendor/tailwind.css — see scripts/build_css.sh).
 *
 *  Content globs include tutor Python files, because some HTML fragments carry
 *  Tailwind classes in Python strings, and the page scripts in
 *  tutor/web/static/*.js (the chat and the map build elements with classes;
 *  plan step 5.5 moved them out of the templates). Any file type that can
 *  contain a class name must be scanned, or the class silently vanishes from
 *  the compiled CSS. The vendored libraries are not scanned.
 */
module.exports = {
  content: [
    "./tutor/web/templates/**/*.html",
    "./tutor/web/static/*.js",
    "./tutor/**/*.py",
  ],
};
