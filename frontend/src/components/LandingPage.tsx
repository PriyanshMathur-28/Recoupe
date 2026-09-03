import { useEffect, useRef, useState } from "react";

/**
 * Marketing landing page converted from the Stitch HTML export.
 *
 * The original relied on inline <script> tags for the WebGL shader background,
 * a three.js hero object, scroll-reveal observers, and a nav blur-on-scroll
 * effect. Those are reimplemented here as React effects:
 *
 *   - The shader background is raw WebGL (no dependency) matching the original
 *     fragment shader verbatim.
 *   - The three.js hero object loads three.js from CDN on demand; if the script
 *     fails to load the hero simply stays empty rather than breaking the page.
 *   - IntersectionObserver drives `.lp-reveal` and a scroll listener toggles the
 *     nav background.
 *
 * Soothing motion is layered on top:
 *   - A cursor spotlight (`--lp-mx/--lp-my`) and ambient drifting orbs.
 *   - Card tilt/parallax (`--lp-rx/--lp-ry/--lp-gx/--lp-gy`) on `.lp-tilt` cards.
 *   - A seamless integration marquee (duplicated track).
 *
 * Every CTA ("Login" and "Start Free", plus the hero button) points at the
 * Flask `/login` route as requested.
 */

const LOGIN_URL = "/login";
const DASHBOARD_IMG =
    "https://lh3.googleusercontent.com/aida/AEtjO1XFt2nYkGK9qio_UwLKIQitdkMqE3tlVNcfc3AoOTmML4VPtKN1qZSh7fxsw6yJY_bxE_80UZryCO-LN-SoZgFRmNS5BS1d30uF9ZRg4xk2qhNK2cfuAJ4i0lY6xql93bMmn_rX45W-vZ7R6FH74IfpN82nm--M1hLUcjcz6-b-blnf6HFq2a773ewtwwzrBUV-uq1c7kYeIRJWnRATkhbf7DTegSkNozmJin0eyLh_n1ZUrJR71QRK";

const INTEGRATIONS = [""];

const VERTEX_SHADER = `attribute vec2 a_position;
    varying vec2 v_texCoord;
    void main() {
    v_texCoord = a_position * 0.5 + 0.5;
    gl_Position = vec4(a_position, 0.0, 1.0);
    }`;

const FRAGMENT_SHADER = `precision highp float;
    uniform float u_time;
    uniform vec2 u_resolution;
    uniform vec2 u_mouse;
    varying vec2 v_texCoord;

    void main() {
        vec2 uv = v_texCoord;
        vec2 mouse = u_mouse / u_resolution;

        vec3 color1 = vec3(0.058, 0.160, 0.133);
        vec3 color2 = vec3(0.976, 0.976, 1.0);

        float t = u_time * 0.15;

        float n = sin(uv.x * 2.5 + t) * cos(uv.y * 1.8 - t * 0.4);
        n += 0.4 * sin(uv.y * 4.0 + t * 0.7) * cos(uv.x * 3.5 + t);
        n += 0.2 * sin((uv.x + uv.y) * 8.0 + t);

        float dist = distance(uv, mouse);
        float glow = smoothstep(0.5, 0.0, dist) * 0.08;

        float mixFactor = smoothstep(-1.5, 1.5, n + (uv.x + uv.y) * 0.2 + glow);
        vec3 finalColor = mix(color1, color2, mixFactor * 0.1 + 0.9);

        gl_FragColor = vec4(finalColor, 1.0);
    }`;

const prefersReducedMotion = () =>
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function useShaderBackground(canvasRef: React.RefObject<HTMLCanvasElement | null>) {
    useEffect(() => {
        const canvas = canvasRef.current;
        if (!canvas) return;
        const gl = (canvas.getContext("webgl") || canvas.getContext("experimental-webgl")) as WebGLRenderingContext | null;
        if (!gl) return;

        const syncSize = () => {
            const w = canvas.clientWidth || 1280;
            const h = canvas.clientHeight || 720;
            if (canvas.width !== w || canvas.height !== h) {
                canvas.width = w;
                canvas.height = h;
            }
        };
        const resizeObserver = typeof ResizeObserver !== "undefined" ? new ResizeObserver(syncSize) : null;
        resizeObserver?.observe(canvas);
        syncSize();

        const compile = (type: number, src: string) => {
            const shader = gl.createShader(type)!;
            gl.shaderSource(shader, src);
            gl.compileShader(shader);
            return shader;
        };
        const program = gl.createProgram()!;
        gl.attachShader(program, compile(gl.VERTEX_SHADER, VERTEX_SHADER));
        gl.attachShader(program, compile(gl.FRAGMENT_SHADER, FRAGMENT_SHADER));
        gl.linkProgram(program);
        gl.useProgram(program);

        const buffer = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
        const position = gl.getAttribLocation(program, "a_position");
        gl.enableVertexAttribArray(position);
        gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);

        const uTime = gl.getUniformLocation(program, "u_time");
        const uRes = gl.getUniformLocation(program, "u_resolution");
        const uMouse = gl.getUniformLocation(program, "u_mouse");

        const mouse = { x: canvas.width / 2, y: canvas.height / 2 };
        const onMouseMove = (event: MouseEvent) => {
            const rect = canvas.getBoundingClientRect();
            if (rect.width && rect.height) {
                const nx = (event.clientX - rect.left) / rect.width;
                const ny = 1.0 - (event.clientY - rect.top) / rect.height;
                mouse.x = nx * canvas.width;
                mouse.y = ny * canvas.height;
            }
        };
        window.addEventListener("mousemove", onMouseMove);

        let frame = 0;
        const render = (t: number) => {
            if (!resizeObserver) syncSize();
            gl.viewport(0, 0, canvas.width, canvas.height);
            if (uTime) gl.uniform1f(uTime, t * 0.001);
            if (uRes) gl.uniform2f(uRes, canvas.width, canvas.height);
            if (uMouse) gl.uniform2f(uMouse, mouse.x, mouse.y);
            gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
            frame = requestAnimationFrame(render);
        };
        frame = requestAnimationFrame(render);

        return () => {
            cancelAnimationFrame(frame);
            resizeObserver?.disconnect();
            window.removeEventListener("mousemove", onMouseMove);
        };
    }, [canvasRef]);
}

function loadThree(): Promise<any> {
    const win = window as unknown as { THREE?: unknown; __threeLoader?: Promise<any> };
    if (win.THREE) return Promise.resolve(win.THREE);
    if (win.__threeLoader) return win.__threeLoader;
    win.__threeLoader = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = "https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js";
        script.async = true;
        script.onload = () => resolve((window as unknown as { THREE: unknown }).THREE);
        script.onerror = reject;
        document.head.appendChild(script);
    });
    return win.__threeLoader;
}

function useHeroScene(containerRef: React.RefObject<HTMLDivElement | null>) {
    useEffect(() => {
        const container = containerRef.current;
        if (!container) return;
        let disposed = false;
        let frame = 0;
        let onResize: (() => void) | null = null;

        loadThree()
            .then((THREE) => {
                if (disposed || !THREE || !container) return;
                const width = container.clientWidth || window.innerWidth;
                const height = container.clientHeight || 600;

                const scene = new THREE.Scene();
                const camera = new THREE.PerspectiveCamera(75, width / height, 0.1, 1000);
                const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
                renderer.setSize(width, height);
                renderer.setPixelRatio(window.devicePixelRatio);
                container.appendChild(renderer.domElement);

                const agentGroup = new THREE.Group();
                scene.add(agentGroup);

                const core = new THREE.Mesh(
                    new THREE.IcosahedronGeometry(1, 15),
                    new THREE.MeshPhongMaterial({ color: 0x0f2922, emissive: 0x0f2922, emissiveIntensity: 0.2, transparent: true, opacity: 0.9, shininess: 100 })
                );
                agentGroup.add(core);

                for (let i = 0; i < 3; i++) {
                    const ring = new THREE.Mesh(
                        new THREE.TorusGeometry(1.5 + i * 0.4, 0.01, 16, 100),
                        new THREE.MeshBasicMaterial({ color: 0x0f2922, transparent: true, opacity: 0.3 - i * 0.05 })
                    );
                    ring.rotation.x = Math.random() * Math.PI;
                    ring.rotation.y = Math.random() * Math.PI;
                    agentGroup.add(ring);
                }

                const coords: number[] = [];
                for (let i = 0; i < 200; i++) {
                    coords.push((Math.random() - 0.5) * 6, (Math.random() - 0.5) * 6, (Math.random() - 0.5) * 6);
                }
                const particlesGeom = new THREE.BufferGeometry();
                particlesGeom.setAttribute("position", new THREE.Float32BufferAttribute(coords, 3));
                agentGroup.add(new THREE.Points(particlesGeom, new THREE.PointsMaterial({ color: 0x0f2922, size: 0.02, transparent: true, opacity: 0.5 })));

                scene.add(new THREE.AmbientLight(0xffffff, 0.6));
                const pointLight = new THREE.PointLight(0xffffff, 1);
                pointLight.position.set(5, 5, 5);
                scene.add(pointLight);

                camera.position.z = 5;

                const animate = (t: number) => {
                    if (disposed) return;
                    agentGroup.rotation.y += 0.003;
                    agentGroup.rotation.x += 0.002;
                    const scale = 1 + Math.sin(t * 0.001) * 0.05;
                    core.scale.set(scale, scale, scale);
                    renderer.render(scene, camera);
                    frame = requestAnimationFrame(animate);
                };

                onResize = () => {
                    const w = container.clientWidth || window.innerWidth;
                    const h = container.clientHeight || 600;
                    camera.aspect = w / h;
                    camera.updateProjectionMatrix();
                    renderer.setSize(w, h);
                };
                window.addEventListener("resize", onResize);
                frame = requestAnimationFrame(animate);

                return () => {
                    renderer.dispose();
                    if (renderer.domElement.parentNode === container) container.removeChild(renderer.domElement);
                };
            })
            .catch(() => {
                /* three.js unavailable — hero visual stays empty, page still works */
            });

        return () => {
            disposed = true;
            cancelAnimationFrame(frame);
            if (onResize) window.removeEventListener("resize", onResize);
        };
    }, [containerRef]);
}

function useRevealAndNav(navRef: React.RefObject<HTMLElement | null>) {
    useEffect(() => {
        const observer = new IntersectionObserver(
            (entries, obs) => {
                entries.forEach((entry) => {
                    if (entry.isIntersecting) {
                        entry.target.classList.add("lp-reveal-active");
                        obs.unobserve(entry.target);
                    }
                });
            },
            { threshold: 0.15 }
        );
        document.querySelectorAll(".lp-reveal").forEach((el) => observer.observe(el));

        const onScroll = () => {
            const nav = navRef.current;
            if (!nav) return;
            nav.classList.toggle("lp-nav-scrolled", window.scrollY > 10);
        };
        window.addEventListener("scroll", onScroll, { passive: true });
        onScroll();

        return () => {
            observer.disconnect();
            window.removeEventListener("scroll", onScroll);
        };
    }, [navRef]);
}

/**
 * Follows the cursor to place the fixed spotlight glow by writing
 * `--lp-mx`/`--lp-my` onto the landing root. Skipped when the visitor prefers
 * reduced motion.
 */
function useCursorSpotlight(rootRef: React.RefObject<HTMLDivElement | null>) {
    useEffect(() => {
        const root = rootRef.current;
        if (!root || prefersReducedMotion()) return;

        const onMove = (event: MouseEvent) => {
            root.style.setProperty("--lp-mx", `${event.clientX}px`);
            root.style.setProperty("--lp-my", `${event.clientY}px`);
        };
        window.addEventListener("mousemove", onMove, { passive: true });
        return () => window.removeEventListener("mousemove", onMove);
    }, [rootRef]);
}

/**
 * Wires pointer-driven tilt/parallax on every `.lp-tilt` card. The card leans
 * toward the cursor via `--lp-rx`/`--lp-ry` and a soft highlight follows via
 * `--lp-gx`/`--lp-gy`. Disabled under reduced-motion.
 */
function useCardTilt(rootRef: React.RefObject<HTMLDivElement | null>) {
    useEffect(() => {
        const root = rootRef.current;
        if (!root || prefersReducedMotion()) return;

        const cards = Array.from(root.querySelectorAll<HTMLElement>(".lp-tilt"));
        const MAX_TILT = 6; // degrees

        const cleanups = cards.map((card) => {
            const onMove = (event: MouseEvent) => {
                const rect = card.getBoundingClientRect();
                const px = (event.clientX - rect.left) / rect.width;
                const py = (event.clientY - rect.top) / rect.height;
                const ry = (px - 0.5) * 2 * MAX_TILT;
                const rx = -(py - 0.5) * 2 * MAX_TILT;
                card.style.setProperty("--lp-ry", `${ry.toFixed(2)}deg`);
                card.style.setProperty("--lp-rx", `${rx.toFixed(2)}deg`);
                card.style.setProperty("--lp-gx", `${(px * 100).toFixed(2)}%`);
                card.style.setProperty("--lp-gy", `${(py * 100).toFixed(2)}%`);
            };
            const onLeave = () => {
                card.style.setProperty("--lp-rx", "0deg");
                card.style.setProperty("--lp-ry", "0deg");
            };
            card.addEventListener("mousemove", onMove);
            card.addEventListener("mouseleave", onLeave);
            return () => {
                card.removeEventListener("mousemove", onMove);
                card.removeEventListener("mouseleave", onLeave);
            };
        });

        return () => cleanups.forEach((fn) => fn());
    }, [rootRef]);
}

/**
 * Centered sign-in overlay converted from the provided Tailwind/HTML export.
 *
 * Rendered as a hover/modal over the landing page (not a separate route or
 * tab). Opening it locks body scroll; it closes on backdrop click, the close
 * button, or the Escape key. On submit it hands off to the Flask `/login`
 * route so the real authentication flow still runs.
 */
function SignInModal({ onClose }: { onClose: () => void }) {
    const [showPassword, setShowPassword] = useState(false);
    const error =
        typeof window !== "undefined" &&
        new URLSearchParams(window.location.search).get("login") === "failed";

    useEffect(() => {
        const onKey = (event: KeyboardEvent) => {
            if (event.key === "Escape") onClose();
        };
        document.addEventListener("keydown", onKey);
        const previousOverflow = document.body.style.overflow;
        document.body.style.overflow = "hidden";
        return () => {
            document.removeEventListener("keydown", onKey);
            document.body.style.overflow = previousOverflow;
        };
    }, [onClose]);

    return (
        <div
            className="lp-signin-overlay"
            role="dialog"
            aria-modal="true"
            aria-label="Sign in to Recoup"
            onMouseDown={(event) => {
                if (event.target === event.currentTarget) onClose();
            }}
        >
            <div className="lp-signin-card">
                <button
                    className="lp-signin-close"
                    type="button"
                    aria-label="Close sign in"
                    onClick={onClose}
                >
                    <span className="material-symbols-outlined">close</span>
                </button>

                <div className="lp-signin-accent" aria-hidden="true" />

                <div className="lp-signin-header">
                    <div className="lp-signin-brand">
                        <span className="material-symbols-outlined lp-signin-brand-icon" style={{ fontVariationSettings: "'FILL' 1" }}>security</span>
                        <span className="lp-signin-brand-name">Recoup</span>
                    </div>
                    <h2 className="lp-signin-title">Welcome back</h2>
                    <p className="lp-signin-subtitle">Sign in to manage your automated recovery.</p>
                </div>

                {error && (
                    <div className="lp-signin-error" role="alert">
                        Invalid credentials. Please try again.
                    </div>
                )}

                <form className="lp-signin-form" method="post" action={LOGIN_URL}>
                    <div className="lp-signin-field">
                        <label className="lp-signin-label" htmlFor="lp-signin-email">Email Address</label>
                        <div className="lp-signin-input-wrap">
                            <span className="material-symbols-outlined lp-signin-input-icon">mail</span>
                            <input
                                className="lp-signin-input"
                                id="lp-signin-email"
                                name="username"
                                type="text"
                                placeholder="name@company.com"
                                autoComplete="username"
                                required
                            />
                        </div>
                    </div>

                    <div className="lp-signin-field">
                        <div className="lp-signin-label-row">
                            <label className="lp-signin-label" htmlFor="lp-signin-password">Password</label>
                            <a className="lp-signin-forgot" href="#">Forgot password?</a>
                        </div>
                        <div className="lp-signin-input-wrap">
                            <span className="material-symbols-outlined lp-signin-input-icon">lock</span>
                            <input
                                className="lp-signin-input lp-signin-input-password"
                                id="lp-signin-password"
                                name="password"
                                type={showPassword ? "text" : "password"}
                                placeholder="••••••••"
                                required
                            />
                            <button
                                className="lp-signin-toggle"
                                type="button"
                                aria-label="Toggle password visibility"
                                onClick={() => setShowPassword((value: boolean) => !value)}
                            >
                                <span className="material-symbols-outlined">
                                    {showPassword ? "visibility" : "visibility_off"}
                                </span>
                            </button>
                        </div>
                    </div>

                    <div className="lp-signin-remember">
                        <input className="lp-signin-checkbox" id="lp-signin-remember" name="remember" type="checkbox" />
                        <label htmlFor="lp-signin-remember">Remember me for 30 days</label>
                    </div>

                    <button className="lp-signin-submit" type="submit">
                        Sign In
                        <span className="material-symbols-outlined" style={{ fontSize: 16 }}>arrow_forward</span>
                    </button>
                </form>

                <div className="lp-signin-divider">
                    <span className="lp-signin-divider-line" />
                    <span className="lp-signin-divider-text">Or continue with</span>
                    <span className="lp-signin-divider-line" />
                </div>

                <div className="lp-signin-social">
                    <button className="lp-signin-social-btn" type="button" onClick={() => { window.location.href = "/dashboard"; }}>
                        <img
                            className="lp-signin-social-icon"
                            alt="Google logo"
                            src="https://lh3.googleusercontent.com/aida-public/AB6AXuB93k7X9LpgUUNIfU6H7aF2cvq36aiDe6O3zDDaYYHslcEwzjNUC-IEmKbVV9ufJKua1hyGS1MHJwOxnylpugR_OIYTzXSDFyj2nD757NZ_Dcj3FBdw6QcR0BE25hOfN2feihJ3vf-LuKeEhdLA1eAnuRNtICZUEq8vi9qxhcDQi1cVdOrA_-VZh3n_wBKiTscoR1eFmfu50csWk-GhxX3zalwyJXtZBCZo6MKi8VCjvixh_mf7GvA"
                        />
                        Google
                    </button>
                    <button className="lp-signin-social-btn" type="button" onClick={() => { window.location.href = "/dashboard"; }}>
                        <img
                            className="lp-signin-social-icon"
                            alt="Apple logo"
                            src="https://lh3.googleusercontent.com/aida-public/AB6AXuAALjuyRJ4wF1GAeI4M-I5gZ2Sf4cw7kZhG-Lh-W3d1fjGooaCModImFM2zwpfu8yn9GfXhfn7W-DCpF2s1WQkNiMWKtKmJMQuJA92fnaJPOq9aly_cYiKbmmjLEcRher1a1JUlSz8O9Ftm7fo3ptOI1rGQ8ixknUEclP81bgwfJ6dIUQIBf_9wxFPn5rsqtW-1mxWtsobcDBAT_OdrZjZ-FmiMWvr9KJirngb7aLQcl5wQiTB-Ezs"
                        />
                        Apple
                    </button>
                </div>

                <p className="lp-signin-signup">
                    Don't have an account?{" "}
                    <a href="#">Sign Up</a>
                </p>
            </div>
        </div>
    );
}

export function LandingPage() {
    const rootRef = useRef<HTMLDivElement | null>(null);
    const shaderRef = useRef<HTMLCanvasElement | null>(null);
    const heroRef = useRef<HTMLDivElement | null>(null);
    const navRef = useRef<HTMLElement | null>(null);
    const [signInOpen, setSignInOpen] = useState(
        () =>
            typeof window !== "undefined" &&
            new URLSearchParams(window.location.search).get("login") === "failed"
    );

    useEffect(() => {
        document.body.classList.add("lp-active-body");
        return () => document.body.classList.remove("lp-active-body");
    }, []);

    useShaderBackground(shaderRef);
    useHeroScene(heroRef);
    useRevealAndNav(navRef);
    useCursorSpotlight(rootRef);
    useCardTilt(rootRef);

    return (
        <div className="lp" ref={rootRef}>
            <div className="lp-shader" aria-hidden="true">
                <canvas ref={shaderRef} />
            </div>
            <div className="lp-orbs" aria-hidden="true">
                <span className="lp-orb lp-orb-1" />
                <span className="lp-orb lp-orb-2" />
                <span className="lp-orb lp-orb-3" />
            </div>
            <div className="lp-spotlight" aria-hidden="true" />

            <header className="lp-nav" ref={navRef}>
                <div className="lp-nav-left">
                    <a className="lp-brand" href="/">
                        <span className="lp-brand-mark">R</span>
                        Recoup
                    </a>
                    <nav className="lp-nav-links">
                        <a className="lp-nav-link lp-nav-link-active" href="#product">Product</a>
                        <a className="lp-nav-link" href="#how">How it Works</a>
                        <a className="lp-nav-link" href="#pricing">Pricing</a>
                        <a className="lp-nav-link" href="#resources">Resources</a>
                    </nav>
                </div>
                <div className="lp-nav-right">
                    <button
                        className="lp-login-link"
                        type="button"
                        onClick={() => setSignInOpen(true)}
                    >
                        Login
                    </button>
                    <button
                        className="lp-btn-liquid"
                        type="button"
                        onClick={() => setSignInOpen(true)}
                    >
                        Start Free
                        <span className="material-symbols-outlined" style={{ fontSize: 16 }}>arrow_forward</span>
                    </button>
                </div>
            </header>

            <main className="lp-main">
                <section className="lp-container lp-hero" id="product">
                    <div className="lp-hero-copy">
                        <div className="lp-badge-wrap lp-reveal">
                            <span className="lp-badge">
                                Made for Indian clinics, salons &amp; service businesses
                                <span className="lp-badge-shimmer" />
                            </span>
                        </div>
                        <h1 className="lp-hero-title lp-reveal" style={{ transitionDelay: "100ms" }}>
                            Stop losing revenue to no-shows and failed payments.
                        </h1>
                        <p className="lp-hero-sub lp-reveal" style={{ transitionDelay: "200ms" }}>
                            Recoup is an AI agent that watches your calendar and your Razorpay payments 24/7 — automatically following up on missed appointments and failed charges, so you recover revenue you'd otherwise never see.
                        </p>
                        <form
                            className="lp-hero-form lp-reveal"
                            style={{ transitionDelay: "300ms" }}
                            onSubmit={(event) => {
                                event.preventDefault();
                                window.location.href = LOGIN_URL;
                            }}
                        >
                            <input className="lp-hero-input" type="text" placeholder="Connect your Google Calendar &amp; Razorpay to get started" />
                            <button className="lp-btn-primary" type="submit">
                                Start Recovering <span className="material-symbols-outlined" style={{ fontSize: 18 }}>arrow_forward</span>
                            </button>
                        </form>
                        <div className="lp-hero-tags lp-reveal" style={{ transitionDelay: "400ms" }}>
                            <span>Clinics</span>
                            <span>·</span>
                            <span>Salons &amp; Spas</span>
                            <span>·</span>
                            <span>Gyms &amp; Studios</span>
                            <span>·</span>
                            <span>Consultants &amp; Coaches</span>
                        </div>
                    </div>
                    <div className="lp-hero-visual lp-reveal lp-reveal-scale" ref={heroRef} style={{ transitionDelay: "500ms" }} aria-hidden="true" />
                </section>

                <section className="lp-container lp-mock">
                    <div className="lp-mock-frame lp-reveal lp-reveal-scale" style={{ transitionDelay: "600ms" }}>
                        <div className="lp-mock-inner">
                            <img src={DASHBOARD_IMG} alt="Recoup dashboard preview" />
                        </div>
                    </div>
                </section>

                <section className="lp-strip lp-reveal">
                    <div className="lp-marquee">
                        <div className="lp-marquee-track">
                            {INTEGRATIONS.map((name) => (
                                <div className="lp-strip-logo" key={`a-${name}`}>{name}</div>
                            ))}
                        </div>
                        <div className="lp-marquee-track" aria-hidden="true">
                            {INTEGRATIONS.map((name) => (
                                <div className="lp-strip-logo" key={`b-${name}`}>{name}</div>
                            ))}
                        </div>
                    </div>
                </section>

                <section className="lp-container lp-section lp-reveal" id="how">
                    <div className="lp-section-head">
                        <h2>How Recoup Works</h2>
                        <p>Three simple steps to passive revenue recovery.</p>
                    </div>
                    <div className="lp-grid-3">
                        <div className="lp-glass lp-step lp-tilt lp-reveal lp-reveal-left" style={{ transitionDelay: "100ms" }}>
                            <div className="lp-step-icon"><span className="material-symbols-outlined">link</span></div>
                            <h3>1. Integration</h3>
                            <p>Securely link your Google Calendar and payment gateways like Razorpay in under two minutes via OAuth. No code required.</p>
                            <span className="lp-tag">OAuth-secured multi-point syncing</span>
                        </div>
                        <div className="lp-glass lp-step lp-tilt lp-reveal" style={{ transitionDelay: "200ms" }}>
                            <div className="lp-step-icon"><span className="material-symbols-outlined">radar</span></div>
                            <h3>2. Automated Analysis</h3>
                            <p>Our AI continuously ingests webhooks to monitor your schedule and transactions, instantly flagging missed appointments and failed charges with high precision.</p>
                            <span className="lp-tag">Behavioral pattern matching</span>
                        </div>
                        <div className="lp-glass lp-step lp-tilt lp-reveal lp-reveal-right" style={{ transitionDelay: "300ms" }}>
                            <div className="lp-step-icon"><span className="material-symbols-outlined">autorenew</span></div>
                            <h3>3. Smart Recovery</h3>
                            <p>Automated, personalized follow-ups gently nudge clients to rebook or complete payments using magic links.</p>
                            <span className="lp-tag">Dynamic fallback logic</span>
                        </div>
                    </div>
                </section>

                <section className="lp-container lp-features">
                    <div className="lp-feature lp-reveal lp-reveal-left">
                        <div className="lp-feature-copy">
                            <h3>Missed Appointments</h3>
                            <p>No-shows cost you time and money. Recoup's agent syncs bilaterally with Google Calendar. When a slot goes unfulfilled, it triggers a sophisticated workflow—checking client history and instantly engaging them via WhatsApp to reschedule, keeping your calendar optimized without manual intervention.</p>
                            <div className="lp-glass lp-callout">
                                <span className="lp-callout-label">Outcome</span>
                                <span className="lp-callout-value">Average Recovery: 18% of monthly revenue</span>
                            </div>
                            <p className="lp-feature-note">Proprietary slot-matching algorithm reduces vacancy time by 40%</p>
                        </div>
                        <div className="lp-feature-visual">
                            <img src={DASHBOARD_IMG} alt="Calendar recovery visualization" />
                            <div className="lp-feature-visual-overlay" />
                        </div>
                    </div>

                    <div className="lp-feature lp-feature-reverse lp-reveal lp-reveal-right">
                        <div className="lp-feature-visual">
                            <img src={DASHBOARD_IMG} alt="Payment recovery visualization" style={{ objectPosition: "right" }} />
                            <div className="lp-feature-visual-overlay lp-overlay-bl" />
                        </div>
                        <div className="lp-feature-copy">
                            <h3>Failed Payments</h3>
                            <p>Don't let network errors or expired cards reduce your bottom line. We intercept failed Razorpay webhook events in real-time, generate unique fallback checkout sessions, and deliver one-click payment links directly to the customer's preferred channel.</p>
                            <div className="lp-glass lp-callout">
                                <span className="lp-callout-label">Pro-Tip</span>
                                <span className="lp-callout-value">Clients are 3x more likely to pay via instant WhatsApp links.</span>
                            </div>
                            <p className="lp-feature-note">Machine-learning driven retry scheduling based on historical success windows</p>
                        </div>
                    </div>
                </section>

                <section className="lp-statement lp-reveal lp-reveal-scale">
                    <div className="lp-statement-glow" />
                    <h2>"Imagine never chasing a payment or a no-show again."</h2>
                </section>
            </main>

            <footer className="lp-footer">
                <div className="lp-footer-brand">
                    <span className="lp-footer-brand-name">
                        <span className="lp-footer-mark">R</span>
                        Recoup
                    </span>
                    <p>© 2024 Recoup AI. Institutional reliability for Indian service businesses.</p>
                </div>
                <nav className="lp-footer-nav">
                    <a href="#privacy">Privacy Policy</a>
                    <a href="#terms">Terms of Service</a>
                    <a href="#security">Security</a>
                    <a href="#status">Status</a>
                    <a href="#contact">Contact</a>
                </nav>
            </footer>

            {signInOpen && <SignInModal onClose={() => setSignInOpen(false)} />}
        </div>
    );
}
