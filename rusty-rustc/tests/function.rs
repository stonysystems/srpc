use rusty::Function;
use ::std::cell::Cell;
use ::std::mem::{align_of, size_of};
use ::std::rc::Rc;

#[test]
fn empty_and_layout_match_the_cpp_runtime() {
    let callback: Function<dyn Fn(i32, i32)> = Function::default();
    assert!(callback.is_empty());
    assert_eq!(size_of::<Function<dyn Fn(i32, i32)>>(), 48);
    assert_eq!(align_of::<Function<dyn Fn(i32, i32)>>(), 16);
    assert_eq!(size_of::<Function<dyn FnMut()>>(), 48);
    assert_eq!(align_of::<Function<dyn FnMut()>>(), 16);
    assert_eq!(size_of::<Function<dyn FnMut(i32)>>(), 48);
    assert_eq!(align_of::<Function<dyn FnMut(i32)>>(), 16);

    macro_rules! assert_not_auto_trait {
        ($type:ty, $auto_trait:ident) => {{
            trait AmbiguousIfImplemented<Marker> {
                fn marker() {}
            }
            impl<T: ?Sized> AmbiguousIfImplemented<()> for T {}
            impl<T: ?Sized + $auto_trait> AmbiguousIfImplemented<u8> for T {}
            let _ = <$type as AmbiguousIfImplemented<_>>::marker;
        }};
    }
    assert_not_auto_trait!(Function<dyn Fn(i32, i32)>, Send);
    assert_not_auto_trait!(Function<dyn Fn(i32, i32)>, Sync);
    assert_not_auto_trait!(Function<dyn FnMut()>, Send);
    assert_not_auto_trait!(Function<dyn FnMut()>, Sync);
    assert_not_auto_trait!(Function<dyn FnMut(i32)>, Send);
    assert_not_auto_trait!(Function<dyn FnMut(i32)>, Sync);
}

#[test]
fn fn_and_fn_mut_dispatch() {
    let observed = Rc::new(Cell::new((0, 0)));
    let sink = Rc::clone(&observed);
    let callback = Function::<dyn Fn(i32, i32)>::from_callable(move |a, b| {
        sink.set((a, b));
    });
    callback(4, 9);
    assert_eq!(observed.get(), (4, 9));

    let calls = Rc::new(Cell::new(0));
    let counter = Rc::clone(&calls);
    let mut callback = Function::<dyn FnMut()>::from_callable(move || {
        counter.set(counter.get() + 1);
    });
    callback();
    callback();
    assert_eq!(calls.get(), 2);

    let sum = Rc::new(Cell::new(0));
    let accumulator = Rc::clone(&sum);
    let mut callback = Function::<dyn FnMut(i32)>::from_callable(move |value| {
        accumulator.set(accumulator.get() + value);
    });
    callback(7);
    callback(5);
    assert_eq!(sum.get(), 12);
}
