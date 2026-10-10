## Slide 1: Light
type: title
title: Light and Telescopes
notes: Welcome everyone.

---

## Slide 2: Today
type: agenda
title: Today
bullets:
  - The inverse-square law
  - Brightness at a distance
  - Light-gathering power

---

## Slide 3: The inverse-square law
type: content
title: The inverse-square law
bullets:
  - With dust in the way, b <= L / (4 pi d^2)
  - Twice as far is a quarter as bright
body: |
  Light spreads over a sphere: at distance d its area is 4 pi d^2.
notes: Draw the spheres around the star.

---

## Slide 4: Brightness at a distance
type: code
title: Brightness at a distance
bullets:
  - Divide by the square of the distance
code: |
  double brightness(double luminosity, double d) {
    // <divide & square>
  }

---

## Slide 5: Picture
type: diagram-placeholder
title: Light spreading out
body: Diagram of light crossing spheres of growing radius.

---

## Slide 6: Check
type: quiz
title: Check your understanding
bullets:
  - How much dimmer is a star three times as far?
