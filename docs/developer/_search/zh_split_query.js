/*
 * Query splitter for the Chinese search page (see _search_language in conf.py).
 *
 * The index was segmented by jieba at build time, but the browser has no
 * segmenter: Sphinx's default splitter breaks a query only at spaces and
 * punctuation, so an unspaced query such as 排空栅栏 is one term that no
 * indexed word equals, and the search finds nothing. This splits each run of
 * Han characters into the longest words the index itself holds, left to
 * right. A character that starts no indexed word is dropped, since a search
 * requires every term and no single character is indexed. Other text splits
 * as Sphinx's default does.
 */
var splitQuery = (query) => {
  const index = (typeof Search !== "undefined" && Search._index) || {};
  const known = (word) =>
    (index.terms && word in index.terms) ||
    (index.titleterms && word in index.titleterms);
  const longest = 8;
  const out = [];
  query
    .split(/[^\p{Letter}\p{Number}_\p{Emoji_Presentation}]+/gu)
    .filter((term) => term)
    .forEach((term) => {
      term.match(/\p{Script=Han}+|\P{Script=Han}+/gu).forEach((run) => {
        if (!/\p{Script=Han}/u.test(run) || known(run)) {
          out.push(run);
          return;
        }
        const words = [];
        let start = 0;
        while (start < run.length) {
          let end = Math.min(run.length, start + longest);
          while (end - start > 1 && !known(run.slice(start, end))) end -= 1;
          if (end - start > 1) words.push(run.slice(start, end));
          start = end;
        }
        // Nothing indexed matched: keep the run, so the page says there are
        // no results rather than searching for nothing.
        out.push(...(words.length ? words : [run]));
      });
    });
  return out;
};
