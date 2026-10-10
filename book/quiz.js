// Lightweight self-check quiz widget for the course book.
// Quizzes are authored as HTML in .qmd files. This script hooks click handlers
// and reveals the explanation. No frameworks, vanilla DOM only.

(function () {
  function init() {
    document.querySelectorAll('.quiz').forEach(quiz => {
      const answer = quiz.dataset.answer; // e.g. "b"
      const options = quiz.querySelectorAll('li');
      options.forEach(li => {
        const choice = li.dataset.choice;
        li.addEventListener('click', () => {
          if (quiz.classList.contains('revealed')) return;
          quiz.classList.add('revealed');
          if (choice === answer) li.classList.add('correct');
          else {
            li.classList.add('wrong');
            // also highlight the correct one
            quiz.querySelector(`li[data-choice="${answer}"]`).classList.add('correct');
          }
        });
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
