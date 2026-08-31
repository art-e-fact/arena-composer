//! PyO3 wrapper. Deliberately thin: Python's job is to resolve the layout and
//! read the USD, this crate's job is to draw the result, and the packed payload
//! is the whole contract between them — the same one `arena-viewer` reads off
//! disk.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::scene::Scene;

/// Open a window on a packed scene, blocking until it is closed.
///
/// `payload` is what `arena_composer.payload.encode()` returns. Must be called
/// from the main thread: the windowing layer requires it.
///
/// The GIL is released for the duration via `py.detach` — the scene is decoded
/// out of the buffer first, so nothing inside the event loop touches Python and
/// other threads in the host interpreter keep running.
#[pyfunction]
fn run_viewer(py: Python<'_>, payload: &[u8]) -> PyResult<()> {
    let scene =
        Scene::from_msgpack(payload).map_err(|e| PyValueError::new_err(format!("{e:#}")))?;
    py.detach(|| crate::viewer::run(scene));
    Ok(())
}

/// The extension module is `arena_composer._core`. PyO3 takes the init symbol
/// from this function's name, so it has to match the last component of
/// `module-name` in `pyproject.toml`.
#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(run_viewer, m)?)?;
    Ok(())
}
