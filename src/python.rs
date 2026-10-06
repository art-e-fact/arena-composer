//! PyO3 wrapper. Deliberately thin: Python's job is to resolve layouts and read
//! the USD, this crate's job is to draw what comes out, and the packed payload
//! is the whole contract between them — the same one `arena-viewer` reads off
//! disk.

use std::path::PathBuf;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::layout::View;
use crate::scene::Scene;

/// Draw the scenes arriving on stdin, blocking until the window is closed.
///
/// Each is a packed payload behind its length (see [`crate::wire`]); the first
/// opens the window, later ones replace what is in it, and the end of the
/// stream closes it. Must be called from the main thread: the windowing layer
/// requires it.
///
/// The GIL is released for every wait via `py.detach` — scenes are decoded off
/// the stream rather than out of Python, so nothing inside the event loop
/// touches the interpreter and other threads in the host keep running.
#[pyfunction]
fn run_viewer_stdin(py: Python<'_>) -> PyResult<()> {
    let first = py
        .detach(|| crate::wire::read_scene(&mut std::io::stdin()))
        .map_err(|e| PyValueError::new_err(format!("{e:#}")))?
        .ok_or_else(|| PyValueError::new_err("no scene arrived on stdin"))?;

    // `std::io::stdin()` is one shared buffered handle, so the reader thread
    // picks up exactly where the read above left off.
    let (sender, updates) = std::sync::mpsc::channel();
    std::thread::spawn(move || crate::wire::forward_scenes(std::io::stdin(), sender));

    py.detach(|| crate::viewer::run(first, updates));
    Ok(())
}

/// Draw one packed scene from each named view, writing a PNG per `shots` entry.
///
/// `shots` pairs a view name — `default`, `top`, `front` or `side` — with the
/// file to write. Blocks until every image is on disk, then returns; there is
/// no window, so this needs no display server. Like the viewer it must own the
/// process: run it in a child (`arena_composer.render`), not in a server.
#[pyfunction]
#[pyo3(signature = (payload, shots, width, height))]
fn render_scene(
    py: Python<'_>,
    payload: &[u8],
    shots: Vec<(String, PathBuf)>,
    width: u32,
    height: u32,
) -> PyResult<()> {
    let scene = Scene::from_msgpack(payload).map_err(|e| PyValueError::new_err(format!("{e:#}")))?;
    let shots = shots
        .into_iter()
        .map(|(view, path)| Ok((view.parse::<View>().map_err(PyValueError::new_err)?, path)))
        .collect::<PyResult<Vec<_>>>()?;

    // Nothing below touches the interpreter, and a render takes seconds.
    py.detach(|| crate::render::run(scene, shots, width, height));
    Ok(())
}

/// The extension module is `arena_composer._core`. PyO3 takes the init symbol
/// from this function's name, so it has to match the last component of
/// `module-name` in `pyproject.toml`.
#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(run_viewer_stdin, m)?)?;
    m.add_function(wrap_pyfunction!(render_scene, m)?)?;
    Ok(())
}
