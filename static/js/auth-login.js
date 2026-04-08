document.addEventListener("DOMContentLoaded", function () {
    const splash = document.querySelector("[data-splash]");
    const loginSection = document.getElementById("login-section");
    const about = document.getElementById("about-section");
    const lines = Array.from(document.querySelectorAll(".about-line"));
    const countTargets = document.querySelectorAll("[data-count-to]");
    const loginCard = document.querySelector("[data-login-card]");
    const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const shouldOpenLoginSection = window.location.hash === "#login-section";
    const heroSection = document.querySelector(".hero-section");
    const floatingCards = Array.from(document.querySelectorAll(".hero-section .floating-image"));

    function intersects(rectA, rectB, gap = 0) {
        return !(
            rectA.right + gap < rectB.left ||
            rectA.left - gap > rectB.right ||
            rectA.bottom + gap < rectB.top ||
            rectA.top - gap > rectB.bottom
        );
    }

    function randomBetween(min, max) {
        return min + Math.random() * (max - min);
    }

    function placeFloatingCards() {
        if (!heroSection || !floatingCards.length) {
            return;
        }
        const heroRect = heroSection.getBoundingClientRect();
        const placed = [];
        const title = heroSection.querySelector(".hero-title");
        const subtitle = heroSection.querySelector(".hero-subtitle");
        const stats = heroSection.querySelector(".hero-micro-stats");
        const storyRow = heroSection.querySelector(".hero-story-row");
        const cta = heroSection.querySelector(".hero-scroll-cta");
        const focalElements = [title, subtitle, stats, storyRow, cta].filter(Boolean);
        const focalRect = focalElements.reduce(
            (bounds, element) => {
                const rect = element.getBoundingClientRect();
                return {
                    left: Math.min(bounds.left, rect.left),
                    top: Math.min(bounds.top, rect.top),
                    right: Math.max(bounds.right, rect.right),
                    bottom: Math.max(bounds.bottom, rect.bottom),
                };
            },
            {
                left: heroRect.left + heroRect.width * 0.25,
                top: heroRect.top + 80,
                right: heroRect.right - heroRect.width * 0.25,
                bottom: heroRect.top + heroRect.height - 140,
            }
        );
        const centerGap = 56;
        const sideInset = 14;
        const topInset = 82;
        const loginRect = loginSection ? loginSection.getBoundingClientRect() : null;
        const safeBottom = loginRect
            ? Math.max(topInset + 140, Math.min(heroRect.height - 24, loginRect.top - heroRect.top - 36))
            : Math.max(topInset + 140, heroRect.height - 36);
        const focalLeft = Math.max(sideInset + 110, focalRect.left - heroRect.left - centerGap);
        const focalRight = Math.min(heroRect.width - sideInset - 110, focalRect.right - heroRect.left + centerGap);
        const regions = [
            {
                xMin: sideInset,
                xMax: Math.max(sideInset + 1, focalLeft - 18),
                yMin: topInset,
                yMax: safeBottom,
            },
            {
                xMin: Math.max(sideInset, focalRight + 18),
                xMax: Math.max(sideInset + 1, heroRect.width - sideInset),
                yMin: topInset,
                yMax: safeBottom,
            },
        ];

        floatingCards.forEach((card, index) => {
            const width = Math.round(randomBetween(88, 112));
            const height = Math.round(width * randomBetween(1.2, 1.38));
            let bestSpot = null;

            card.style.width = `${width}px`;
            card.style.height = `${height}px`;

            const preferredRegions = [regions[index % 2], regions[(index + 1) % 2]];

            for (const region of preferredRegions) {
                const xMin = region.xMin;
                const xMax = Math.max(xMin, region.xMax - width);
                const yMin = region.yMin;
                const yMax = Math.max(yMin, region.yMax - height);

                for (let tries = 0; tries < 120; tries += 1) {
                    const x = randomBetween(xMin, xMax);
                    const y = randomBetween(yMin, yMax);
                    const candidate = {
                        left: x,
                        top: y,
                        right: x + width,
                        bottom: y + height,
                    };

                    const collides = placed.some((rect) => intersects(candidate, rect, -22));
                    if (collides) {
                        continue;
                    }

                    bestSpot = candidate;
                    break;
                }

                if (bestSpot) {
                    break;
                }
            }

            if (!bestSpot) {
                const fallbackRegion = preferredRegions[0];
                const xMin = fallbackRegion.xMin;
                const xMax = Math.max(xMin, fallbackRegion.xMax - width);
                const yMin = fallbackRegion.yMin;
                const yMax = Math.max(yMin, fallbackRegion.yMax - height);
                bestSpot = {
                    left: randomBetween(xMin, xMax),
                    top: randomBetween(yMin, yMax),
                    right: 0,
                    bottom: 0,
                };
                bestSpot.right = bestSpot.left + width;
                bestSpot.bottom = bestSpot.top + height;
            }

            placed.push(bestSpot);
            card.style.left = `${bestSpot.left}px`;
            card.style.top = `${bestSpot.top}px`;
            card.style.setProperty("--float-rot", `${randomBetween(-16, 16).toFixed(2)}deg`);
            card.style.setProperty("--float-lift", `${randomBetween(10, 22).toFixed(2)}px`);
            card.style.setProperty("--float-shift-x", `${randomBetween(-12, 12).toFixed(2)}px`);
            card.style.setProperty("--float-duration", `${randomBetween(5.8, 10.4).toFixed(2)}s`);
            card.style.animationDelay = `${randomBetween(-6, 0).toFixed(2)}s`;
            card.style.zIndex = `${10 + index}`;
        });
    }

    function scrollToLoginSection() {
        if (!loginSection) {
            return;
        }

        const header = document.querySelector(".header");

        window.requestAnimationFrame(() => {
            const sectionRect = loginSection.getBoundingClientRect();
            const currentScroll = window.scrollY || window.pageYOffset;
            const headerHeight = header ? header.offsetHeight + 24 : 0;
            const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
            const centeredTop = currentScroll + sectionRect.top - Math.max((viewportHeight - sectionRect.height) / 2, headerHeight);
            const targetTop = Math.max(centeredTop - 12, 0);

            window.scrollTo({
                top: targetTop,
                behavior: prefersReducedMotion ? "auto" : "smooth",
            });
        });
    }

    if (splash) {
        if (prefersReducedMotion || shouldOpenLoginSection) {
            splash.classList.add("is-hidden");
            if (shouldOpenLoginSection) {
                window.setTimeout(scrollToLoginSection, 40);
            }
        } else {
            window.setTimeout(() => {
                splash.classList.add("is-hidden");
            }, 1600);
        }
    }

    placeFloatingCards();
    let resizeTimer = null;
    window.addEventListener("resize", () => {
        if (resizeTimer) {
            window.clearTimeout(resizeTimer);
        }
        resizeTimer = window.setTimeout(placeFloatingCards, 120);
    });

    const loginAnchors = document.querySelectorAll('a[href="#login-section"], a[href$="#login-section"]');
    loginAnchors.forEach((anchor) => {
        anchor.addEventListener("click", function (event) {
            const href = anchor.getAttribute("href") || "";
            const isSamePageAnchor = href === "#login-section";

            if (!isSamePageAnchor) {
                return;
            }

            event.preventDefault();

            if (splash) {
                splash.classList.add("is-hidden");
            }

            history.replaceState(null, "", "#login-section");
            scrollToLoginSection();
        });
    });

    if (countTargets.length) {
        const statObserver = new IntersectionObserver(
            (entries, observer) => {
                entries.forEach((entry) => {
                    if (!entry.isIntersecting) {
                        return;
                    }

                    animateCount(entry.target, Number(entry.target.dataset.countTo || 0), prefersReducedMotion ? 0 : 1200);
                    observer.unobserve(entry.target);
                });
            },
            { threshold: 0.5 }
        );

        countTargets.forEach((target) => statObserver.observe(target));
    }

    if (loginCard && !prefersReducedMotion) {
        loginCard.addEventListener("mousemove", (event) => {
            const rect = loginCard.getBoundingClientRect();
            const px = (event.clientX - rect.left) / rect.width;
            const py = (event.clientY - rect.top) / rect.height;
            const rotateY = (px - 0.5) * 6;
            const rotateX = (0.5 - py) * 5;

            loginCard.style.transform = `perspective(1200px) rotateX(${rotateX}deg) rotateY(${rotateY}deg) translateY(-4px)`;
        });

        loginCard.addEventListener("mouseleave", () => {
            loginCard.style.transform = "";
        });
    }

    if (!about || !lines.length) {
        return;
    }

    let started = false;

    function typeLine(element, text, speed) {
        return new Promise((resolve) => {
            let index = 0;
            const timer = window.setInterval(() => {
                index += 1;
                element.textContent = text.slice(0, index);

                if (index >= text.length) {
                    window.clearInterval(timer);
                    element.classList.remove("typing");
                    resolve();
                }
            }, speed);
        });
    }

    async function startTyping() {
        if (started) {
            return;
        }

        started = true;

        for (const line of lines) {
            const text = line.dataset.text || "";
            line.classList.add("typing");
            await typeLine(line, text, 20);
            await new Promise((resolve) => window.setTimeout(resolve, 180));
        }
    }

    const observer = new IntersectionObserver(
        (entries) => {
            entries.forEach((entry) => {
                if (entry.isIntersecting) {
                    startTyping();
                    observer.disconnect();
                }
            });
        },
        { threshold: 0.35 }
    );

    observer.observe(about);
});

function animateCount(element, endValue, duration) {
    if (!element) return;
    if (!duration || duration <= 0) {
        element.textContent = `${endValue}`;
        return;
    }

    const start = performance.now();

    function frame(now) {
        const progress = Math.min((now - start) / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        element.textContent = `${Math.round(endValue * eased)}`;

        if (progress < 1) {
            requestAnimationFrame(frame);
        }
    }

    requestAnimationFrame(frame);
}
