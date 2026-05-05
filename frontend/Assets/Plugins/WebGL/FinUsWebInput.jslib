mergeInto(LibraryManager.library, {
  FinUsPromptTextInput: function (gameObjectNamePtr, currentValuePtr) {
    var gameObjectName = UTF8ToString(gameObjectNamePtr);
    var currentValue = UTF8ToString(currentValuePtr);

    setTimeout(function () {
      var nextValue = window.prompt("종목을 입력하세요.", currentValue || "");
      if (nextValue === null) {
        return;
      }

      if (window.finusUnityInstance && window.finusUnityInstance.SendMessage) {
        window.finusUnityInstance.SendMessage(gameObjectName, "OnWebStockInputChanged", nextValue);
      }
    }, 0);
  }
});
