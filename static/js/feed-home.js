document.addEventListener("DOMContentLoaded", function () {
    if (window.location.hash === "#login-section") {
        history.replaceState(null, "", window.location.pathname + window.location.search);
    }

    const posts = [...document.querySelectorAll(".likeable-post")];
    const videos = [...document.querySelectorAll(".post-video")];
    const musicCards = [...document.querySelectorAll(".post-music-card")];
    const musicAudios = [...document.querySelectorAll(".post-music-audio")];
    const header = document.querySelector(".header");

    function setupPostSnapScroll() {
        const postCards = [...document.querySelectorAll(".feed.feed-only .post")];
        if (!postCards.length) return;

        let locked = false;
        let lastWheelAt = 0;
        const lockMs = 420;
        const wheelThreshold = 18;

        function getCurrentIndex() {
            const probeY = (window.innerHeight * 0.5) + (window.scrollY || 0);
            let bestIndex = 0;
            let bestDistance = Number.POSITIVE_INFINITY;
            postCards.forEach((el, index) => {
                const centerY = window.scrollY + el.getBoundingClientRect().top + (el.offsetHeight / 2);
                const distance = Math.abs(centerY - probeY);
                if (distance < bestDistance) {
                    bestDistance = distance;
                    bestIndex = index;
                }
            });
            return bestIndex;
        }

        function scrollToPost(index) {
            if (index < 0 || index >= postCards.length) return;
            const topInset = (header?.offsetHeight || 0) + 18;
            const top = Math.max(postCards[index].offsetTop - topInset, 0);
            window.scrollTo({ top, behavior: "smooth" });
        }

        window.addEventListener(
            "wheel",
            (event) => {
                if (Math.abs(event.deltaY) < wheelThreshold) return;
                if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
                if (event.target instanceof Element && event.target.closest("textarea, input, select, video")) return;

                const now = Date.now();
                if (locked || now - lastWheelAt < 120) {
                    event.preventDefault();
                    return;
                }

                const current = getCurrentIndex();
                const next = event.deltaY > 0 ? Math.min(current + 1, postCards.length - 1) : Math.max(current - 1, 0);

                if (next === current) return;

                event.preventDefault();
                locked = true;
                lastWheelAt = now;
                scrollToPost(next);
                window.setTimeout(() => {
                    locked = false;
                }, lockMs);
            },
            { passive: false }
        );
    }

    async function postAction(postEl, action) {
        const busy = action === "like" ? "liking" : "saving";
        if (!postEl || postEl.dataset[busy] === "true") return null;
        postEl.dataset[busy] = "true";
        try {
            const res = await fetch(`/${action}_post/${postEl.dataset.postId}`, {
                method: "POST",
                headers: { "X-Requested-With": "XMLHttpRequest" },
            });
            const data = await res.json();
            return res.ok && data.success ? data : null;
        } catch (err) {
            console.error(`${action} failed`, err);
            return null;
        } finally {
            postEl.dataset[busy] = "false";
        }
    }

    function burstHeart(containerEl, x, y) {
        const rect = containerEl.getBoundingClientRect();
        const heart = Object.assign(document.createElement("span"), {
            className: "like-burst-heart",
            textContent: "\u2665",
        });
        heart.style.left = `${x - rect.left}px`;
        heart.style.top = `${y - rect.top}px`;
        containerEl.appendChild(heart);
        window.setTimeout(() => heart.remove(), 850);
    }

    function escapeHtml(text) {
        const span = document.createElement("span");
        span.textContent = text ?? "";
        return span.innerHTML;
    }

    function syncCommentButton(form) {
        const input = form?.querySelector('input[name="comment"]');
        const submit = form?.querySelector(".comment-submit-btn");
        const active = !!input?.value?.trim();
        if (submit) {
            submit.disabled = !active;
            submit.classList.toggle("is-active", active);
        }
    }

    function toggleComments(postEl, shouldFocusInput = false) {
        const section = document.getElementById(`comments-${postEl.dataset.postId}`);
        if (!section) return;

        if (shouldFocusInput) {
            section.classList.remove("is-hidden");
            postEl.querySelector('.comment-form input[name="comment"]')?.focus();
            return;
        }

        section.classList.toggle("is-hidden");
    }

    function ensureViewCommentsControl(postEl, count) {
        const stack = postEl.querySelector(".post-social-stack");
        if (!stack) return null;

        const commentLabel = count === 1 ? "comment" : "comments";
        let control = stack.querySelector(".view-comments-btn");
        const wantsButton = count > 2;
        const previewList = stack.querySelector(".comment-preview-list");

        if (wantsButton && (!control || !control.classList.contains("toggle-comments"))) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "view-comments-btn toggle-comments";
            button.dataset.postId = postEl.dataset.postId || "";
            if (previewList) {
                stack.insertBefore(button, previewList);
            } else {
                stack.appendChild(button);
            }
            if (control) control.remove();
            control = button;
        }

        if (!wantsButton && (!control || control.classList.contains("toggle-comments"))) {
            const label = document.createElement("div");
            label.className = "view-comments-btn view-comments-label";
            if (previewList) {
                stack.insertBefore(label, previewList);
            } else {
                stack.appendChild(label);
            }
            if (control) control.remove();
            control = label;
        }

        if (control) {
            control.innerHTML = wantsButton
                ? `View all <span class="comment-count">${count}</span> ${commentLabel}`
                : `<span class="comment-count">${count}</span> ${commentLabel}`;

            if (control.classList.contains("toggle-comments") && !control.dataset.bound) {
                control.dataset.bound = "true";
                control.addEventListener("click", () => toggleComments(postEl, false));
            }
        }

        return control;
    }

    function appendCommentPreview(postEl, comment) {
        const stack = postEl.querySelector(".post-social-stack");
        if (!stack) return;

        let previewList = stack.querySelector(".comment-preview-list");
        if (!previewList) {
            previewList = document.createElement("div");
            previewList.className = "comment-preview-list";
            stack.appendChild(previewList);
        }

        const preview = document.createElement("div");
        preview.className = "comment-inline";
        preview.innerHTML = `<strong>${escapeHtml(comment.username)}</strong><span>${escapeHtml(comment.text)}</span>`;
        previewList.appendChild(preview);

        while (previewList.children.length > 2) {
            previewList.firstElementChild?.remove();
        }
    }

    posts.forEach((postEl) => {
        const mediaTarget = postEl.querySelector(".post-media-frame") || postEl;
        const commentForm = postEl.querySelector(".comment-form");
        const commentInput = commentForm?.querySelector('input[name="comment"]');

        commentInput?.addEventListener("input", () => syncCommentButton(commentForm));
        syncCommentButton(commentForm);

        postEl.querySelectorAll(".toggle-comments").forEach((btn) => {
            btn.addEventListener("click", () => {
                toggleComments(postEl, btn.classList.contains("comment-icon-btn"));
            });
        });

        postEl.querySelector(".like-form")?.addEventListener("submit", async (event) => {
            event.preventDefault();
            const data = await postAction(postEl, "like");
            if (!data) return;

            postEl.dataset.liked = data.liked ? "true" : "false";
            const likeBtn = postEl.querySelector(".like-form .like-icon-btn");
            if (likeBtn) {
                likeBtn.classList.toggle("liked", !!data.liked);
            }

            const count = postEl.querySelector(".like-count");
            if (count) count.textContent = data.likes_count;
        });

        postEl.querySelector(".save-form")?.addEventListener("submit", async (event) => {
            event.preventDefault();
            const data = await postAction(postEl, "save");
            const saveBtn = postEl.querySelector(".save-form .save-icon-btn");
            if (!data || !saveBtn) return;
            saveBtn.classList.toggle("saved", !!data.saved);
        });

        postEl.querySelector(".comment-form")?.addEventListener("submit", async (event) => {
            event.preventDefault();
            const form = event.currentTarget;
            const input = form.querySelector('input[name="comment"]');
            const section = document.getElementById(`comments-${postEl.dataset.postId}`);
            const action = form.getAttribute("action");
            const text = input?.value?.trim();
            if (!action || !text) return;

            try {
                const res = await fetch(action, {
                    method: "POST",
                    headers: { "X-Requested-With": "XMLHttpRequest" },
                    body: new URLSearchParams({ comment: text }),
                });
                const data = await res.json();
                if (!res.ok || !data.success) return;

                if (section) {
                    const node = document.createElement("div");
                    node.className = "comment";
                    node.innerHTML = `
                        <div class="comment-header">
                            <strong>${escapeHtml(data.comment.username)}</strong>
                            <small>${escapeHtml(data.comment.created_at_label || "Just now")}</small>
                        </div>
                        <div class="comment-text">${escapeHtml(data.comment.text)}</div>
                    `;
                    section.appendChild(node);
                    section.classList.remove("is-hidden");
                }

                if (typeof data.comments_count === "number") {
                    ensureViewCommentsControl(postEl, data.comments_count);
                }

                appendCommentPreview(postEl, data.comment);

                if (input) {
                    input.value = "";
                }
                syncCommentButton(form);
            } catch (err) {
                console.error("Comment failed", err);
            }
        });

        mediaTarget.addEventListener("dblclick", (event) => {
            burstHeart(mediaTarget, event.clientX, event.clientY);
            if (postEl.dataset.liked !== "true") {
                postEl.querySelector(".like-form")?.requestSubmit();
            }
        });
    });

    setupPostSnapScroll();

    function setMusicToggleState(audio, isMuted) {
        const card = audio.closest(".post-music-card");
        const button = card?.querySelector(".post-music-toggle");
        if (!button) return;
        button.dataset.state = isMuted ? "muted" : "unmuted";
        button.setAttribute("aria-pressed", String(!isMuted));
        button.textContent = isMuted ? "Unmute" : "Mute";
    }

    function pauseOtherMusic(activeAudio) {
        musicAudios.forEach((audio) => {
            if (audio !== activeAudio) {
                audio.pause();
                audio.currentTime = 0;
                audio.muted = true;
                setMusicToggleState(audio, true);
            }
        });
    }

    if (musicAudios.length) {
        const musicObserver = new IntersectionObserver(
            (entries) => {
                entries.forEach(({ target, isIntersecting, intersectionRatio }) => {
                    if (!(target instanceof HTMLElement)) return;
                    const audio = target.querySelector(".post-music-audio");
                    if (!(audio instanceof HTMLAudioElement)) return;

                    if (isIntersecting && intersectionRatio >= 0.72) {
                        pauseOtherMusic(audio);
                        audio.muted = true;
                        setMusicToggleState(audio, true);
                        audio.play().catch(() => {});
                        return;
                    }

                    if (!isIntersecting || intersectionRatio < 0.4) {
                        audio.pause();
                    }
                });
            },
            { threshold: [0, 0.4, 0.72, 1] }
        );

        musicCards.forEach((card) => {
            musicObserver.observe(card);
        });

        musicAudios.forEach((audio) => {
            setMusicToggleState(audio, true);

            audio.addEventListener("play", () => {
                pauseOtherMusic(audio);
            });

            const card = audio.closest(".post-music-card");
            const toggleBtn = card?.querySelector(".post-music-toggle");
            toggleBtn?.addEventListener("click", () => {
                if (audio.paused) {
                    pauseOtherMusic(audio);
                    audio.play().catch(() => {});
                }

                const shouldUnmute = audio.muted;
                if (shouldUnmute) {
                    pauseOtherMusic(audio);
                }
                audio.muted = !shouldUnmute ? true : false;
                setMusicToggleState(audio, audio.muted);
            });
        });
    }

    if (videos.length) {
        const pauseOffscreen = new IntersectionObserver(
            (entries) => {
                entries.forEach(({ target, intersectionRatio }) => {
                    if (target instanceof HTMLVideoElement && intersectionRatio < 0.45 && !target.paused) {
                        target.pause();
                    }
                });
            },
            { threshold: [0, 0.45, 1] }
        );

        videos.forEach((video) => {
            pauseOffscreen.observe(video);
            video.addEventListener("play", () => {
                videos.forEach((other) => other !== video && !other.paused && other.pause());
            });
        });
    }
});
