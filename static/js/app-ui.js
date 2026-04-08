document.addEventListener("DOMContentLoaded", function () {
    const flashWrap = document.querySelector(".flash-messages");

    function dismissAlert(alertEl) {
        if (!alertEl || alertEl.classList.contains("is-dismissing")) return;
        alertEl.classList.add("is-dismissing");
        window.setTimeout(() => {
            alertEl.remove();
            if (flashWrap && !flashWrap.querySelector(".alert")) {
                flashWrap.remove();
            }
        }, 230);
    }

    if (flashWrap) {
        flashWrap.addEventListener("click", (event) => {
            const closeBtn = event.target.closest(".close-alert");
            if (!closeBtn) return;
            const alertEl = closeBtn.closest(".alert");
            dismissAlert(alertEl);
        });

        flashWrap.querySelectorAll(".alert").forEach((alertEl) => {
            const isError = alertEl.classList.contains("alert-error") || alertEl.classList.contains("alert-danger");
            const timeout = isError ? 7000 : 4200;
            window.setTimeout(() => dismissAlert(alertEl), timeout);
        });
    }

    const revealTargets = document.querySelectorAll(
        ".post, .profile-card, .create-card, .auth-container, .hero-section, .reveal-sequence, .showcase-stat, .story-bubble, .feature-card"
    );

    revealTargets.forEach((el, index) => {
        el.classList.add("reveal");
        el.style.setProperty("--reveal-delay", `${Math.min(index * 70, 420)}ms`);
    });

    const revealObserver = new IntersectionObserver(
        (entries) => {
            entries.forEach((entry) => {
                if (entry.isIntersecting) {
                    entry.target.classList.add("is-visible");
                    revealObserver.unobserve(entry.target);
                }
            });
        },
        { threshold: 0.12 }
    );

    revealTargets.forEach((el) => revealObserver.observe(el));
});

(function () {
    const lb = document.getElementById("lightbox");
    const img = document.getElementById("lightbox-img");
    const prevBtn = document.querySelector(".lightbox-prev");
    const nextBtn = document.querySelector(".lightbox-next");
    let images = [];
    let idx = 0;

    if (!lb || !img) {
        return;
    }

    function openLightbox(urls, startIndex) {
        images = urls || [];
        idx = typeof startIndex === "number" ? startIndex : 0;
        if (!images.length) return;
        img.src = images[idx];
        lb.classList.add("open");
        lb.setAttribute("aria-hidden", "false");
        document.body.style.overflow = "hidden";
    }

    function closeLightbox() {
        lb.classList.remove("open");
        lb.setAttribute("aria-hidden", "true");
        img.src = "";
        images = [];
        document.body.style.overflow = "";
    }

    function showNext() {
        if (!images.length) return;
        idx = (idx + 1) % images.length;
        img.src = images[idx];
    }

    function showPrev() {
        if (!images.length) return;
        idx = (idx - 1 + images.length) % images.length;
        img.src = images[idx];
    }

    document.addEventListener("click", function (e) {
        const target = e.target;

        if (target && target.classList && target.classList.contains("open-full")) {
            e.preventDefault();
            const data = target.getAttribute("data-images");
            let urls = [];

            if (data) {
                urls = data.split(",").map((s) => s.trim()).filter(Boolean);
            }

            if (!urls.length) {
                const source = target.getAttribute("data-src") || target.src;
                if (source) urls = [source];
            }

            let start = 0;
            const startSrc = target.getAttribute("data-src") || target.src;
            if (startSrc && urls.length) {
                const found = urls.indexOf(startSrc);
                if (found >= 0) start = found;
            }

            openLightbox(urls, start);
            return;
        }

        if (target && (target.id === "lightbox" || target.classList.contains("lightbox-close"))) {
            closeLightbox();
            return;
        }

        if (target && target.classList && target.classList.contains("lightbox-prev")) {
            showPrev();
            return;
        }

        if (target && target.classList && target.classList.contains("lightbox-next")) {
            showNext();
        }
    });

    if (prevBtn) {
        prevBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            showPrev();
        });
    }

    if (nextBtn) {
        nextBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            showNext();
        });
    }

    document.addEventListener("keydown", function (e) {
        if (!lb.classList.contains("open")) return;
        if (e.key === "Escape") closeLightbox();
        if (e.key === "ArrowRight") showNext();
        if (e.key === "ArrowLeft") showPrev();
    });
})();

(function () {
    const modal = document.getElementById("profile-preview-modal");
    const previewImg = document.getElementById("profile-preview-img");
    const closeBtn = document.querySelector(".profile-preview-close");
    const previewSelector = ".avatar, .post-owner img, .feed-suggestion-item img, .saved-gallery-user img, .connections-user img";

    if (!modal || !previewImg) return;

    function openModal(src, alt) {
        if (!src) return;
        previewImg.src = src;
        previewImg.alt = alt || "Profile preview";
        modal.classList.add("open");
        modal.setAttribute("aria-hidden", "false");
        document.body.style.overflow = "hidden";
    }

    function closeModal() {
        modal.classList.remove("open");
        modal.setAttribute("aria-hidden", "true");
        previewImg.src = "";
        document.body.style.overflow = "";
    }

    document.addEventListener("click", (event) => {
        const target = event.target;
        if (!(target instanceof Element)) return;

        const profileImg = target.closest(previewSelector);
        if (profileImg instanceof HTMLImageElement) {
            openModal(profileImg.currentSrc || profileImg.src, profileImg.alt);
            return;
        }

        if (target === modal || target.closest(".profile-preview-close")) {
            closeModal();
        }
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && modal.classList.contains("open")) closeModal();
    });

    if (closeBtn) closeBtn.addEventListener("click", closeModal);
})();

